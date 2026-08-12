"""One client, one run — light or full (task-35 §3.3).

Orchestrates every other `app/monitor/*` module: fetch -> parse -> fingerprint
-> diff -> persist. Not a `SyncJob` (task-35 §3.1) — a plain function called
by both the scheduler (`app/monitor/scheduler.py`) and the CLI/API "Check
now" path, each supplying their own short-lived `Session`.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.models import Client
from app.db.repositories.dimensions import DimensionRepository
from app.db.repositories.monitor import MonitorRepository
from app.monitor import diff as diffmod
from app.monitor import dns as dnsmod
from app.monitor import fingerprint as fp
from app.monitor import page as pagemod
from app.monitor import robots as robotsmod
from app.monitor import severity as sev
from app.monitor import sitemaps as sitemapsmod
from app.monitor import storage
from app.monitor import tls as tlsmod
from app.monitor.fetcher import FetchResult, FetchStats, crawl_delay_floor, fetch, host_of
from app.tools.http_safety import HostThrottle

logger = logging.getLogger(__name__)

LIGHT = "light"
FULL = "full"

# `monitor_client_workers` (default 4, task-35 §3.6) write against the same
# SQLite file concurrently, from separate threads of the SAME worker process.
# SQLite only ever allows one writer at a time regardless, and its lock is
# only released on COMMIT — so every write AND the commit that follows it
# must be one uninterrupted critical section, not just the commit call: a
# thread that flushes, releases, and commits later would let another thread
# start writing while its transaction is still open, which is exactly what
# live measurement on 2026-08-11 caught as "database is locked" even with an
# earlier version of this lock that only wrapped the bare `commit()`.
# `PRAGMA busy_timeout` (app/db/session.py) is not a substitute here: it
# retries a blocked *statement*, it does not prevent one thread's write from
# starting inside another thread's still-open transaction in the first place.
#
# This only serializes monitor-vs-monitor writes within this process; the
# much lower-frequency cross-process case (the job dispatcher, or the API
# process enqueueing a run) still relies on `busy_timeout`, exactly as it
# already does today without this module.
_COMMIT_LOCK = threading.Lock()


# task-35 §3.4: which change types represent an ongoing condition that can
# self-heal (vs. a one-off fact like `title_changed`, which has nothing to
# "resolve" — the new title just IS the new title now).
_ROBOTS_TRACKED = frozenset({"robots_blocks_site", "robots_unavailable", "robots_missing"})
_PAGE_TRACKED = frozenset(
    {"page_noindex_appeared", "page_status_error", "gsc_verification_lost", "analytics_tag_lost"}
)
_SITEMAP_RESOURCE_TRACKED = frozenset({"sitemap_lost"})
_SITEMAP_URL_TRACKED = frozenset(
    {"sitemap_url_redirects", "sitemap_url_broken", "sitemap_url_unavailable"}
)
_SITEMAP_AGGREGATE_TRACKED = frozenset({"sitemap_broken_urls"})
# Split in two, each reconciled only against the pass that could possibly
# produce it: `gsc_verification_lost` only comes out of the TXT record-type
# pass, `dns_unavailable` only out of the final any-record-type-failed pass.
# Reconciling either against an unrelated pass's (necessarily empty)
# `detected` list would resolve it moments after it was opened.
_DNS_VERIFICATION_TRACKED = frozenset({"gsc_verification_lost"})
_DNS_UNAVAILABLE_TRACKED = frozenset({"dns_unavailable"})
_TLS_TRACKED = frozenset({"tls_expired", "tls_expiring_3d", "tls_expiring_14d"})


@dataclass
class RunOutcome:
    run_id: str
    status: str
    urls_checked: int
    truncated: bool


def _apply_changes(
    repo: MonitorRepository,
    client_id: str,
    run_id: str,
    changes: list[diffmod.DetectedChange],
    *,
    now: datetime,
) -> int:
    """Record every newly-detected change, skipping any that are already
    open for the same `(change_type, subject)` at the SAME severity — the
    ongoing condition is already visible, and a fresh row every run would be
    spam, not signal.

    A severity change while still open (e.g. `sitemap_broken_urls` going
    warning -> critical as more of a sitemap breaks) is not spam: the old row
    is resolved and a new one opened at the new severity, so the feed shows
    exactly when it got worse (or better) rather than freezing at whichever
    level first tripped it.
    """
    written = 0
    for change in changes:
        existing = repo.open_change(client_id, change.change_type, change.subject)
        if existing is not None:
            if existing.severity == change.severity:
                continue
            repo.resolve_change(existing, now=now)
        repo.record_change(
            client_id,
            run_id=run_id,
            severity=change.severity,
            change_type=change.change_type,
            scope=change.scope,
            subject=change.subject,
            old_value=change.old_value,
            new_value=change.new_value,
            details=change.details,
            now=now,
        )
        written += 1
    return written


def _reconcile(
    repo: MonitorRepository,
    client_id: str,
    run_id: str,
    subject: str,
    *,
    tracked_types: frozenset[str],
    detected: list[diffmod.DetectedChange],
    now: datetime,
) -> None:
    """Self-healing (task-35 §3.4): any tracked type that is open for this
    subject but was NOT detected this run gets `resolved_at` + an `info`
    `<type>_resolved` event.
    """
    detected_types = {c.change_type for c in detected if c.subject == subject}
    for change_type in tracked_types - detected_types:
        open_change = repo.open_change(client_id, change_type, subject)
        if open_change is None:
            continue
        repo.resolve_change(open_change, now=now)
        repo.record_change(
            client_id,
            run_id=run_id,
            severity=sev.INFO,
            change_type=sev.resolved_change_type(change_type),
            scope=open_change.scope,
            subject=subject,
            now=now,
        )


def _base_url(client: Client, override: str | None) -> str:
    return (override or f"https://{client.domain}").rstrip("/")


def _process_robots(
    repo: MonitorRepository,
    client: Client,
    base_url: str,
    *,
    run_id: str,
    throttle: HostThrottle,
    stats: FetchStats,
    settings: Settings,
    artifacts_dir: Path,
    is_baseline_run: bool,
    now: datetime,
) -> tuple[robotsmod.RobotsDocument, bool]:
    """Returns `(parsed document, was fetched ok)`."""
    url = f"{base_url}/robots.txt"
    result = fetch(
        url,
        throttle=throttle,
        stats=stats,
        timeout=settings.monitor_request_timeout_seconds,
        max_body_bytes=settings.monitor_max_body_bytes,
    )

    with _COMMIT_LOCK:
        resource = repo.get_resource(client.id, url)
        old_structure = resource.structure_json if resource else None
        old_status = resource.status if resource else None
        old_content_sha256 = resource.content_sha256 if resource else None

        if result.body:
            doc = robotsmod.parse_robots(
                pagemod.decode_body(result.body, content_type=result.headers.get("Content-Type"))
            )
        else:
            doc = robotsmod.RobotsDocument()
        new_structure = robotsmod.to_structure(doc)

        content_sha256 = None
        if result.body:
            blob = storage.store_blob(artifacts_dir, result.body)
            repo.record_blob(
                blob.sha256,
                size_bytes=blob.size_bytes,
                gzip_size_bytes=blob.gzip_size_bytes,
                mime="text/plain",
                path=blob.path,
            )
            content_sha256 = blob.sha256

        resource = repo.upsert_resource_state(
            client.id,
            url,
            kind="robots",
            status=result.status,
            content_sha256=content_sha256,
            structure=new_structure,
            missing=(result.status == 404),
            now=now,
        )

        changed_fields = (
            robotsmod.diff_structures(old_structure, new_structure) if not is_baseline_run else []
        )
        if content_sha256 != old_content_sha256:
            prev = repo.latest_resource_version(resource.id)
            repo.create_resource_version(
                resource,
                http_status=result.status,
                content_sha256=content_sha256,
                structure=new_structure,
                changed_fields=changed_fields,
                prev_version_id=prev.id if prev else None,
                observed_at=now,
            )

        old_blocking = (
            robotsmod.blocking_relevant_groups(robotsmod.from_structure(old_structure))
            if old_structure
            else []
        )
        new_blocking = (
            robotsmod.blocking_relevant_groups(doc) if result.status and result.status < 400 else []
        )
        changes = diffmod.diff_robots(
            domain=client.domain,
            old_status=old_status,
            new_status=result.status,
            old_blocking=old_blocking,
            new_blocking=new_blocking,
            changed_group_fields=changed_fields,
            is_baseline_run=is_baseline_run,
        )
        _apply_changes(repo, client.id, run_id, changes, now=now)
        _reconcile(
            repo, client.id, run_id, url, tracked_types=_ROBOTS_TRACKED, detected=changes, now=now
        )

        repo.commit()
        return doc, result.status is not None and result.status < 400


@dataclass(frozen=True)
class _SitemapFetch:
    """One fetched sitemap file: parsed document + the bytes worth keeping.

    `body` is the **decompressed** payload, never the raw `.xml.gz` bytes:
    gzip framing embeds an mtime, so re-gzipping identical XML yields different
    bytes and every run would look like a new version of an unchanged file.
    """

    doc: sitemapsmod.SitemapDocument
    body: bytes | None
    status: int | None


def _fetch_sitemap_tree(
    entry_urls: list[str],
    *,
    throttle: HostThrottle,
    stats: FetchStats,
    settings: Settings,
    max_files: int = 200,
) -> dict[str, _SitemapFetch]:
    """Recurse into a sitemap index, bounded — task-35 §3.3 "аварійний клапан"."""
    seen: dict[str, _SitemapFetch] = {}
    queue = list(dict.fromkeys(entry_urls))
    while queue and len(seen) < max_files:
        url = queue.pop(0)
        if url in seen:
            continue
        result = fetch(
            url,
            throttle=throttle,
            stats=stats,
            timeout=settings.monitor_request_timeout_seconds,
            max_body_bytes=settings.monitor_max_body_bytes,
        )
        if not result.body:
            seen[url] = _SitemapFetch(
                doc=sitemapsmod.SitemapDocument(kind="unknown", entries=()),
                body=None,
                status=result.status,
            )
            continue
        data = sitemapsmod.maybe_decompress(
            result.body, url=url, content_type=result.headers.get("Content-Type")
        )
        doc = sitemapsmod.parse_sitemap(data)
        seen[url] = _SitemapFetch(doc=doc, body=data, status=result.status)
        if doc.kind == "index":
            queue.extend(e.loc for e in doc.entries if e.loc not in seen)
    return seen


# A version's `structure` is what the diff view reads, so the URL list has to
# be in it — but a 25k-URL sitemap would put megabytes of JSON in one row. The
# list is capped and the cut is FLAGGED (never silent, PLAN §12); the complete
# file always survives verbatim in the blob store regardless.
_MAX_STRUCTURE_URLS = 2000


def _sitemap_structure(doc: sitemapsmod.SitemapDocument) -> dict[str, Any]:
    structure: dict[str, Any] = {"kind": doc.kind, "entry_count": len(doc.entries)}
    locs = [entry.loc for entry in doc.entries]
    if doc.kind == "index":
        structure["children"] = locs[:_MAX_STRUCTURE_URLS]
    elif doc.kind == "urlset":
        structure["urls"] = locs[:_MAX_STRUCTURE_URLS]
    if len(locs) > _MAX_STRUCTURE_URLS:
        structure["urls_truncated"] = True
    return structure


def _structure_changed_fields(old: dict[str, Any] | None, new: dict[str, Any]) -> list[str]:
    """Which keys of a resource structure differ — the `changed_fields` the
    diff view highlights. Empty for a first version: there is nothing to
    compare against, and every key would read as "changed"."""
    if not old:
        return []
    return sorted(key for key in set(old) | set(new) if old.get(key) != new.get(key))


def _process_sitemaps(
    repo: MonitorRepository,
    client: Client,
    base_url: str,
    robots_doc: robotsmod.RobotsDocument,
    *,
    run_id: str,
    throttle: HostThrottle,
    stats: FetchStats,
    settings: Settings,
    artifacts_dir: Path,
    is_baseline_run: bool,
    now: datetime,
) -> dict[str, str | None]:
    """Returns the flat map of every URL currently listed anywhere in the
    sitemap tree -> its `lastmod` (or `None`)."""
    entry_urls = list(robots_doc.sitemaps) or [f"{base_url}/sitemap.xml"]
    tree = _fetch_sitemap_tree(entry_urls, throttle=throttle, stats=stats, settings=settings)

    # The tree fetch above touches the network only; every write below goes
    # through the same process-wide lock `_process_page` uses, for the same
    # reason (task-35 §3.6 — serializing SQLite writes under 4-way parallelism).
    all_urls: dict[str, str | None] = {}
    with _COMMIT_LOCK:
        for url, fetched in tree.items():
            doc = fetched.doc
            resource = repo.get_resource(client.id, url)
            old_structure = resource.structure_json if resource else None
            old_content_sha256 = resource.content_sha256 if resource else None
            # A file that answered but was not itself fetched successfully
            # this pass (e.g. queued as a child but the fetch failed) already
            # has kind="unknown" and no entries — treat as missing.
            status = 200 if doc.kind != "unknown" else (resource.status if resource else None)
            missing = doc.kind == "unknown"

            # The file itself is kept, not just its shape: "зберігати всі версії
            # sitemap" (task-35 §2) means the XML has to survive, and the blob
            # store already deduplicates identical bytes.
            content_sha256 = None
            if fetched.body:
                blob = storage.store_blob(artifacts_dir, fetched.body)
                repo.record_blob(
                    blob.sha256,
                    size_bytes=blob.size_bytes,
                    gzip_size_bytes=blob.gzip_size_bytes,
                    mime="application/xml",
                    path=blob.path,
                )
                content_sha256 = blob.sha256

            structure = _sitemap_structure(doc)
            resource = repo.upsert_resource_state(
                client.id,
                url,
                kind="sitemap",
                status=(None if missing else status),
                content_sha256=content_sha256,
                structure=structure,
                missing=missing,
                now=now,
            )

            # Same rule as robots (`_process_robots`): a version is appended
            # only when the stored bytes actually differ, so an unchanged
            # sitemap costs nothing and a disappeared one is recorded as a
            # version with no content rather than silently overwritten.
            if content_sha256 != old_content_sha256:
                changed_fields = (
                    [] if is_baseline_run else _structure_changed_fields(old_structure, structure)
                )
                prev = repo.latest_resource_version(resource.id)
                repo.create_resource_version(
                    resource,
                    http_status=(None if missing else status),
                    content_sha256=content_sha256,
                    structure=structure,
                    changed_fields=changed_fields,
                    prev_version_id=prev.id if prev else None,
                    observed_at=now,
                )

            changes = diffmod.diff_sitemap_resource(
                resource_url=url,
                new_status=(None if missing else status),
                is_baseline_run=is_baseline_run,
            )
            _apply_changes(repo, client.id, run_id, changes, now=now)
            _reconcile(
                repo,
                client.id,
                run_id,
                url,
                tracked_types=_SITEMAP_RESOURCE_TRACKED,
                detected=changes,
                now=now,
            )

            if doc.kind == "urlset":
                for entry in doc.entries:
                    all_urls[entry.loc] = entry.lastmod

        repo.commit()

    return all_urls


def _key_pages(session: Session, client: Client, *, limit: int, base_url: str) -> set[str]:
    """Homepage + top GSC pages by clicks over the trailing 30 days
    (task-35 §1.5) — no new collection, `DimensionRepository.top_pages`
    already has this from the normal weekly sync.
    """
    pages = {f"{base_url}/"}
    end = date.today()
    start = end - timedelta(days=30)
    try:
        rows, _ = DimensionRepository(session).top_pages(client.id, start, end, limit=limit)
    except Exception:  # noqa: BLE001 - GSC data may simply not exist yet for this client
        return pages
    for row in rows:
        if row.dimension_value and row.dimension_value.startswith("http"):
            pages.add(row.dimension_value)
    return pages


def _process_page(
    repo: MonitorRepository,
    client: Client,
    url: str,
    *,
    run_id: str,
    is_key_page: bool,
    in_sitemap: bool,
    throttle: HostThrottle,
    stats: FetchStats,
    settings: Settings,
    artifacts_dir: Path,
    is_baseline_run: bool,
    now: datetime,
) -> tuple[bool, str | None]:
    """Returns `(changed, status_kind)` — `status_kind` feeds the run's
    `urls_new/urls_changed/urls_gone` counters and the sitemap-health count.

    The network fetch happens before the lock below is acquired, and
    everything from there on (state reads, writes, diffing) happens inside
    it: `monitor_client_workers` (default 4) threads hit the same SQLite
    file, which only ever allows one writer, so every DB touch — including a
    plain `session.flush()` inside a repository call, not just the periodic
    `_commit()` — must be serialized process-wide (task-35 §3.6, live-measured
    2026-08-11: unserialized writes under real 4-way concurrency produced
    "database is locked"). Doing the fetch first keeps the slow, genuinely
    parallel part — the network round trip — outside the lock.
    """
    result = fetch(
        url,
        throttle=throttle,
        stats=stats,
        timeout=settings.monitor_request_timeout_seconds,
        max_body_bytes=settings.monitor_max_body_bytes,
    )

    with _COMMIT_LOCK:
        return _persist_page_result(
            repo,
            client,
            url,
            result,
            run_id=run_id,
            is_key_page=is_key_page,
            in_sitemap=in_sitemap,
            settings=settings,
            artifacts_dir=artifacts_dir,
            is_baseline_run=is_baseline_run,
            now=now,
        )


def _persist_page_result(
    repo: MonitorRepository,
    client: Client,
    url: str,
    result: FetchResult,
    *,
    run_id: str,
    is_key_page: bool,
    in_sitemap: bool,
    settings: Settings,
    artifacts_dir: Path,
    is_baseline_run: bool,
    now: datetime,
) -> tuple[bool, str | None]:
    page = repo.get_or_create_page(client.id, url, is_key_page=is_key_page)
    prev_version = repo.latest_page_version(page.id)
    # `google_site_verifications` lives only in the version's `signals_json`
    # (no column on the mutable `MonitorPage` state row), so the "old" value
    # for that one field comes from the previous version, not page state.
    old_gsv = frozenset(
        (prev_version.signals_json or {}).get("google_site_verifications", [])
        if prev_version
        else []
    )
    old_observation = (
        diffmod.PageObservation(
            status=page.status,
            final_url=page.final_url,
            meta_robots=page.meta_robots,
            x_robots_tag=page.x_robots_tag,
            canonical=page.canonical,
            title=page.title,
            description=page.description,
            h1=page.h1,
            indexable=page.indexable,
            analytics_ids=frozenset(page.analytics_ids_json),
            google_site_verifications=old_gsv,
            hop_count=page.hop_count,
        )
        if page.last_checked_at is not None
        else None
    )

    html = (
        pagemod.decode_body(result.body, content_type=result.headers.get("Content-Type"))
        if result.body
        else ""
    )
    signals = pagemod.extract_signals(html) if html else pagemod.PageSignals()
    x_robots_tag = result.headers.get("X-Robots-Tag")
    indexable = pagemod.is_indexable(
        meta_robots=signals.meta_robots, x_robots_tag=x_robots_tag, status=result.status
    )

    signals_dict = fp.build_signals(
        status=result.status,
        final_url=result.final_url,
        meta_robots=signals.meta_robots,
        x_robots_tag=x_robots_tag,
        canonical=signals.canonical,
        title=signals.title,
        description=signals.description,
        h1=signals.h1,
        hreflang=signals.hreflang,
        json_ld_types=signals.json_ld_types,
        analytics_ids=signals.analytics_ids,
        google_site_verifications=signals.google_site_verifications,
        indexable=indexable,
    )
    signals_sha256 = fp.signals_fingerprint(signals_dict)
    text_sha256 = fp.text_fingerprint(signals.visible_text) if result.body else None
    raw_sha256 = fp.raw_fingerprint(result.body) if result.body else None

    fingerprint_changed = (
        prev_version is None
        or signals_sha256 != prev_version.signals_sha256
        or text_sha256 != prev_version.text_sha256
    )
    body_blob_sha256 = None
    if (
        result.body
        and settings.monitor_store_page_bodies
        and (fingerprint_changed or prev_version is None)
    ):
        blob = storage.store_blob(artifacts_dir, result.body)
        repo.record_blob(
            blob.sha256,
            size_bytes=blob.size_bytes,
            gzip_size_bytes=blob.gzip_size_bytes,
            mime=result.headers.get("Content-Type"),
            path=blob.path,
        )
        body_blob_sha256 = blob.sha256

    new_observation = diffmod.PageObservation(
        status=result.status,
        final_url=result.final_url,
        meta_robots=signals.meta_robots,
        x_robots_tag=x_robots_tag,
        canonical=signals.canonical,
        title=signals.title,
        description=signals.description,
        h1=signals.h1,
        indexable=indexable,
        analytics_ids=frozenset(signals.analytics_ids),
        google_site_verifications=frozenset(signals.google_site_verifications),
        hop_count=result.hop_count,
    )

    page = repo.upsert_page_state(
        page,
        status=result.status,
        final_url=result.final_url,
        hop_count=result.hop_count,
        indexable=indexable,
        meta_robots=signals.meta_robots,
        x_robots_tag=x_robots_tag,
        canonical=signals.canonical,
        title=signals.title,
        description=signals.description,
        h1=signals.h1,
        word_count=signals.word_count,
        analytics_ids=signals.analytics_ids,
        signals_sha256=signals_sha256,
        text_sha256=text_sha256,
        raw_sha256=raw_sha256,
        in_sitemap=in_sitemap,
        now=now,
    )

    changed_fields: list[str] = []
    if fingerprint_changed:
        changed_fields = [
            key
            for key in signals_dict
            if not prev_version or (prev_version.signals_json or {}).get(key) != signals_dict[key]
        ]
        repo.create_page_version(
            page,
            http_status=result.status,
            signals=signals_dict,
            signals_sha256=signals_sha256,
            text_sha256=text_sha256,
            raw_sha256=raw_sha256,
            body_blob_sha256=body_blob_sha256,
            changed_fields=changed_fields,
            prev_version_id=prev_version.id if prev_version else None,
            observed_at=now,
        )

    changes = diffmod.diff_page(
        url=url,
        client_domain=client.domain,
        old=old_observation,
        new=new_observation,
        is_key_page=is_key_page,
        is_baseline_run=is_baseline_run,
    )
    changes += diffmod.diff_content(
        url=url,
        old_text_sha256=(prev_version.text_sha256 if prev_version else None),
        new_text_sha256=text_sha256,
        old_raw_sha256=(prev_version.raw_sha256 if prev_version else None),
        new_raw_sha256=raw_sha256,
        is_baseline_run=is_baseline_run,
    )
    _apply_changes(repo, client.id, run_id, changes, now=now)
    _reconcile(repo, client.id, run_id, url, tracked_types=_PAGE_TRACKED, detected=changes, now=now)

    status_kind = sev.classify_sitemap_status(result.status) if in_sitemap else None
    if in_sitemap and status_kind != "ok" and not is_baseline_run:
        change_type, severity = sev.sitemap_url_status_change(
            {"redirect": "3xx", "broken": "4xx", "unavailable": "5xx"}.get(
                status_kind or "5xx", "5xx"
            ),
            is_key_page=is_key_page,
            consecutive_failures=page.consecutive_failures,
        )
        url_changes = [diffmod.DetectedChange(change_type, severity, "sitemap", url)]
        _apply_changes(repo, client.id, run_id, url_changes, now=now)
        _reconcile(
            repo,
            client.id,
            run_id,
            url,
            tracked_types=_SITEMAP_URL_TRACKED,
            detected=url_changes,
            now=now,
        )
    elif in_sitemap and status_kind == "ok":
        _reconcile(
            repo, client.id, run_id, url, tracked_types=_SITEMAP_URL_TRACKED, detected=[], now=now
        )

    is_new = old_observation is None
    is_changed = bool(changed_fields) and not is_new
    repo.commit()
    return (is_new or is_changed), status_kind


def _process_dns(
    repo: MonitorRepository,
    client: Client,
    base_url: str,
    *,
    run_id: str,
    settings: Settings,
    is_baseline_run: bool,
    now: datetime,
) -> None:
    apex = client.domain
    names = (
        {apex, f"www.{apex}"} if not apex.startswith("www.") else {apex, apex.removeprefix("www.")}
    )
    any_unavailable = False

    for name in names:
        for record_type in dnsmod.RECORD_TYPES:
            # DoH lookup is a network call — stays outside the lock. Every
            # write below it shares the same process-wide lock `_process_page`
            # uses (task-35 §3.6).
            result = dnsmod.resolve(
                name, record_type, resolvers=tuple(settings.monitor_doh_resolvers)
            )
            with _COMMIT_LOCK:
                record = repo.get_dns_record(client.id, name, record_type)
                # This specific record's own baseline, not the client's: DNS
                # is only ever checked on `full` runs, so a client whose
                # first-ever run was `light` reaches its first DNS check
                # later, on a run where `is_baseline_run` is already False —
                # without this, that first sight of a real value would read
                # as a change from "nothing" and fire a false
                # `dns_apex_a_changed` critical (live-measured 2026-08-11 on
                # exxxoticaexpo.com's real first full run).
                record_is_new = record is None
                old_values = frozenset(record.values_json) if record else frozenset()
                if not result.available:
                    any_unavailable = True
                    continue

                record = repo.upsert_dns_state(
                    client.id,
                    name,
                    record_type,
                    values=list(result.values),
                    ttl=result.ttl,
                    resolver=result.resolver,
                    now=now,
                )
                if frozenset(result.values) != old_values:
                    prev = repo.latest_dns_version(record.id)
                    repo.create_dns_version(
                        record,
                        values=list(result.values),
                        ttl=result.ttl,
                        resolver=result.resolver,
                        prev_version_id=prev.id if prev else None,
                        observed_at=now,
                    )
                changes = diffmod.diff_dns(
                    client_domain=client.domain,
                    name=name,
                    record_type=record_type,
                    old_values=old_values,
                    new_values=frozenset(result.values),
                    is_baseline_run=is_baseline_run or record_is_new,
                )
                _apply_changes(repo, client.id, run_id, changes, now=now)
                if record_type == "TXT":
                    # Scoped to the TXT pass only: `gsc_verification_lost` can
                    # only ever be produced there. Reconciling it against
                    # every OTHER record type's (necessarily empty) `detected`
                    # list would resolve it moments after `_apply_changes`
                    # opened it.
                    _reconcile(
                        repo,
                        client.id,
                        run_id,
                        name,
                        tracked_types=_DNS_VERIFICATION_TRACKED,
                        detected=changes,
                        now=now,
                    )
                repo.commit()

    dns_changes: list[diffmod.DetectedChange] = []
    with _COMMIT_LOCK:
        if any_unavailable and not is_baseline_run:
            dns_changes = [diffmod.DetectedChange("dns_unavailable", sev.WARNING, "dns", apex)]
            _apply_changes(repo, client.id, run_id, dns_changes, now=now)
        _reconcile(
            repo,
            client.id,
            run_id,
            apex,
            tracked_types=_DNS_UNAVAILABLE_TRACKED,
            detected=dns_changes,
            now=now,
        )
        repo.commit()


def _process_tls(
    repo: MonitorRepository,
    client: Client,
    base_url: str,
    *,
    run_id: str,
    is_baseline_run: bool,
    now: datetime,
) -> None:
    # TLS handshake is a network call — stays outside the lock.
    host = host_of(base_url) or client.domain
    info = tlsmod.check_certificate(host)
    if not info.available:
        return
    days_left = tlsmod.days_until_expiry(info.not_after, now=now)
    result = sev.tls_change(days_left)
    changes: list[diffmod.DetectedChange] = []
    with _COMMIT_LOCK:
        if result is not None and not is_baseline_run:
            change_type, severity = result
            changes = [
                diffmod.DetectedChange(
                    change_type,
                    severity,
                    "site",
                    host,
                    new_value=(info.not_after.date().isoformat() if info.not_after else None),
                    details={"days_left": days_left},
                )
            ]
            _apply_changes(repo, client.id, run_id, changes, now=now)
        _reconcile(
            repo, client.id, run_id, host, tracked_types=_TLS_TRACKED, detected=changes, now=now
        )
        repo.commit()


def _sitemap_aggregate(
    repo: MonitorRepository,
    client: Client,
    *,
    run_id: str,
    settings: Settings,
    now: datetime,
) -> None:
    """No `is_baseline_run` gate, unlike every other `diff*` call in this
    file: task-35 §3.4.1 explicitly exempts this one aggregate from the
    "first run alerts on nothing" rule — a batch of pre-existing broken
    sitemap URLs is a fact worth surfacing on day one, not noise.
    """
    with _COMMIT_LOCK:
        counts = repo.sitemap_health_counts(client.id)
        broken = counts["broken"] + counts["unavailable"] + counts["redirects"]
        severity = sev.sitemap_broken_summary_severity(
            broken,
            counts["total"],
            warn_pct=float(settings.monitor_sitemap_broken_warn_pct),
            warn_count=settings.monitor_sitemap_broken_warn_count,
            alert_pct=float(settings.monitor_sitemap_broken_alert_pct),
            alert_count=settings.monitor_sitemap_broken_alert_count,
        )
        changes: list[diffmod.DetectedChange] = []
        subject = f"{client.domain}/sitemap"
        if severity is not None:
            changes = [
                diffmod.DetectedChange(
                    "sitemap_broken_urls",
                    severity,
                    "sitemap",
                    subject,
                    details={**counts, "broken_total": broken},
                )
            ]
            # Baseline is exempt from the "no alerts on first run" rule for
            # this one aggregate (task-35 §3.4.1: "Це видно вже на
            # baseline" — a batch of pre-existing broken URLs is a fact
            # worth surfacing on day one, unlike per-page "new page" noise).
            _apply_changes(repo, client.id, run_id, changes, now=now)
        _reconcile(
            repo,
            client.id,
            run_id,
            subject,
            tracked_types=_SITEMAP_AGGREGATE_TRACKED,
            detected=changes,
            now=now,
        )
        repo.commit()


def run_client_monitor(
    session: Session,
    client: Client,
    *,
    kind: str,
    settings: Settings,
    artifacts_dir: Path,
    trigger: str = "scheduled",
    now: datetime | None = None,
    stop_event: threading.Event | None = None,
    existing_run_id: str | None = None,
) -> RunOutcome:
    """`existing_run_id` claims an already-`queued` row (the API's "Check
    now" enqueues one; the scheduler claims and runs it — same split as
    `SyncJob`) instead of creating a fresh `running` row.
    """
    now = now or datetime.now(UTC)
    repo = MonitorRepository(session)
    with _COMMIT_LOCK:
        monitor_settings = repo.get_or_create_settings(client.id)
        base_url = _base_url(client, monitor_settings.base_url_override)
        is_baseline_run = repo.latest_finished_run(client.id) is None

        if existing_run_id is not None:
            claimed = repo.claim_queued_run(existing_run_id, now=now)
            run = (
                claimed
                if claimed is not None
                else repo.create_run(client.id, kind=kind, trigger=trigger)
            )
        else:
            run = repo.create_run(client.id, kind=kind, trigger=trigger)
        session.commit()

    crawl_delay = None
    min_interval = crawl_delay_floor(crawl_delay, settings.monitor_host_min_interval_seconds)
    throttle = HostThrottle(min_interval_seconds=min_interval)
    stats = FetchStats()
    deadline = now + timedelta(minutes=settings.monitor_run_max_minutes)

    try:
        robots_doc, robots_ok = _process_robots(
            repo,
            client,
            base_url,
            run_id=run.id,
            throttle=throttle,
            stats=stats,
            settings=settings,
            artifacts_dir=artifacts_dir,
            is_baseline_run=is_baseline_run,
            now=now,
        )
        # `_process_robots` already committed internally (inside its own
        # `_COMMIT_LOCK` — see that function): committing again here, outside
        # any lock, would be exactly the bug this whole locking scheme exists
        # to avoid (task-35 §3.6).

        # Honor Crawl-delay from here on (task-35 §3.3) — re-throttle if the
        # site asked for something slower than our floor.
        found_delay = robotsmod.crawl_delay_for(robots_doc)
        if found_delay and found_delay > min_interval:
            throttle = HostThrottle(min_interval_seconds=found_delay)

        sitemap_urls = _process_sitemaps(
            repo,
            client,
            base_url,
            robots_doc,
            run_id=run.id,
            throttle=throttle,
            stats=stats,
            settings=settings,
            artifacts_dir=artifacts_dir,
            is_baseline_run=is_baseline_run,
            now=now,
        )
        # Also already committed internally by `_process_sitemaps`.

        max_urls = monitor_settings.max_urls or settings.monitor_max_urls_per_client
        key_pages = _key_pages(
            session, client, limit=settings.monitor_key_pages_limit, base_url=base_url
        )

        if kind == LIGHT:
            # Only *critical* pages earn an hourly look between full runs
            # (task-35 §3.3 point 3) — filtering by `scope == "page"` alone
            # is not enough: `list_changes` without a severity filter also
            # returns `info`-level history markers (`page_new`,
            # `content_changed`, ...) which never resolve, so almost every
            # page ever seen would count as "open" and the light run would
            # balloon to the entire portfolio (live-measured 2026-08-11: hit
            # `list_changes`'s default `limit=100` on every client, turning
            # a ~30s/client check into a ~60-100s one).
            open_alert_urls = {
                c.subject
                for c in repo.list_changes(client.id, state="open", severity="critical", limit=1000)
                if c.scope == "page"
            }
            targets = sorted(key_pages | open_alert_urls)
            target_in_sitemap = {url: (url in sitemap_urls) for url in targets}
        else:
            all_pages = repo.pages_in_ring_order(client.id)
            known_gone_ok = {
                p.url
                for p in all_pages
                if p.gone_since is not None
                and (now - p.gone_since) < timedelta(days=settings.monitor_gone_retention_days)
            }
            wanted_urls = set(sitemap_urls) | known_gone_ok | key_pages
            ordered_existing = [p.url for p in all_pages if p.url in wanted_urls]
            new_urls = sorted(wanted_urls - set(ordered_existing))
            targets = (ordered_existing + new_urls)[:max_urls]
            target_in_sitemap = {url: (url in sitemap_urls) for url in targets}

        with _COMMIT_LOCK:
            repo.update_run_counters(run.id, urls_total=len(targets))
            session.commit()

        checked = 0
        changed_count = 0
        new_count = 0
        gone_count = 0
        truncated = False
        interrupted = False
        for url in targets:
            # "Зупинка: stop_event перевіряється між URL, тож SIGTERM не
            # чекає кінець обходу" (task-35 §3.6).
            if stop_event is not None and stop_event.is_set():
                truncated = True
                interrupted = True
                break
            if datetime.now(UTC) >= deadline:
                truncated = True
                break
            is_new_page = repo.get_page(client.id, url) is None
            changed, _status_kind = _process_page(
                repo,
                client,
                url,
                run_id=run.id,
                is_key_page=(url in key_pages),
                in_sitemap=target_in_sitemap.get(url, False),
                throttle=throttle,
                stats=stats,
                settings=settings,
                artifacts_dir=artifacts_dir,
                is_baseline_run=is_baseline_run,
                now=now,
            )
            checked += 1
            if is_new_page:
                new_count += 1
            elif changed:
                changed_count += 1
            if checked % 10 == 0:
                with _COMMIT_LOCK:
                    repo.update_run_counters(
                        run.id,
                        urls_checked=checked,
                        requests_made=stats.requests_made,
                        bytes_downloaded=stats.bytes_downloaded,
                    )
                    session.commit()

        if kind == FULL and not interrupted:
            # Anything previously known but no longer wanted this pass (fell
            # out of the sitemap AND past its grace period) is truly gone.
            with _COMMIT_LOCK:
                all_pages = repo.all_pages(client.id)
                for page in all_pages:
                    still_in_sitemap = page.url in sitemap_urls
                    if not still_in_sitemap and page.in_sitemap:
                        page.in_sitemap = False
                        repo.mark_page_gone(page, now=now)
                    elif still_in_sitemap and page.gone_since is not None:
                        repo.clear_page_gone(page)
                gone_count = sum(
                    1
                    for p in all_pages
                    if p.gone_since is not None and (now - p.gone_since) < timedelta(seconds=1)
                )
                session.commit()

            # `_process_dns`/`_process_tls`/`_sitemap_aggregate` each commit
            # internally, inside their own `_COMMIT_LOCK` (task-35 §3.6).
            _process_dns(
                repo,
                client,
                base_url,
                run_id=run.id,
                settings=settings,
                is_baseline_run=is_baseline_run,
                now=now,
            )
            _process_tls(
                repo, client, base_url, run_id=run.id, is_baseline_run=is_baseline_run, now=now
            )
            _sitemap_aggregate(repo, client, run_id=run.id, settings=settings, now=now)
        elif is_baseline_run and not interrupted:
            # Even a `light` first run should surface the baseline sitemap
            # health fact (task-35 §3.4.1) rather than waiting up to 3h.
            _sitemap_aggregate(repo, client, run_id=run.id, settings=settings, now=now)

        with _COMMIT_LOCK:
            # What THIS run found, not what is currently open client-wide
            # (task-37 F4) — a light run on 1 URL must not report the same
            # numbers as a full run on 609.
            counts_by_severity = repo.count_changes_by_severity(run.id)
            remaining = len(targets) - checked if truncated else None
            repo.finish_run(
                run.id,
                status="failed_interrupted" if interrupted else "succeeded",
                truncated=truncated,
                remaining_count=remaining,
                notes={"baseline": is_baseline_run},
            )
            repo.update_run_counters(
                run.id,
                urls_checked=checked,
                urls_changed=changed_count,
                urls_new=new_count,
                urls_gone=gone_count,
                requests_made=stats.requests_made,
                bytes_downloaded=stats.bytes_downloaded,
                changes_critical=counts_by_severity.get("critical", 0),
                changes_warning=counts_by_severity.get("warning", 0),
                changes_info=counts_by_severity.get("info", 0),
            )
            session.commit()
        return RunOutcome(
            run_id=run.id, status="succeeded", urls_checked=checked, truncated=truncated
        )

    except Exception as exc:  # noqa: BLE001 - one client's failure must never crash the scheduler
        session.rollback()
        logger.exception("monitor_run_failed client_id=%s kind=%s", client.id, kind)
        repo = MonitorRepository(session)
        with _COMMIT_LOCK:
            repo.finish_run(
                run.id, status="failed", error_code="run_failed", error_message=str(exc)[:900]
            )
            session.commit()
        return RunOutcome(run_id=run.id, status="failed", urls_checked=0, truncated=False)
