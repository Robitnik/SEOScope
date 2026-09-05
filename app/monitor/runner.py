"""One client, one run — light or full (task-35 §3.3).

Orchestrates every other `app/monitor/*` module: fetch -> parse -> fingerprint
-> diff -> persist. Not a `SyncJob` (task-35 §3.1) — a plain function called
by both the scheduler (`app/monitor/scheduler.py`) and the CLI/API "Check
now" path, each supplying their own short-lived `Session`.
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from dataclasses import replace as dataclass_replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.models import Client, MonitorPage, MonitorSettings
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
from app.monitor.browser_fetch import BrowserFetchSession, fetch_via_browser
from app.monitor.fetcher import FetchResult, FetchStats, crawl_delay_floor, fetch, host_of
from app.monitor.redirects import collect as redirects_collect
from app.tools.http_safety import HostThrottle

logger = logging.getLogger(__name__)

LIGHT = "light"
FULL = "full"
PAGE = "page"

# One page checked at a time — `run_single_page_check` fires a real outbound
# fetch to the client's site, so a double-click (or two operators clicking
# the same row) must get a 409, not a second concurrent request racing the
# first (task-38 §2, precedent: `claim_target` in task-29). A run-scoped or
# per-process lock is enough here: unlike `_COMMIT_LOCK`, this guards a
# single page's OWN check for its own duration, not every DB write.
_PAGE_CHECK_IN_FLIGHT: set[str] = set()
_PAGE_CHECK_GUARD = threading.Lock()


class PageCheckConflict(Exception):
    """A check for this exact page is already running."""

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

# task-58 §1.3: ONE process-wide cap on concurrently-live Chromium instances,
# shared by every client's run and every page worker inside it — replaces
# the old dedicated `_antibot_pool` (one worker, one client at a time). A
# page-worker thread's `_LazyBrowser` acquires a slot on first actual need
# and holds it for that worker's whole run, so this bounds "how many
# Chromiums exist right now across the whole portfolio", not "how many
# clients are on the browser lane" (a client can have several page workers
# each holding a slot). Sized lazily from `Settings` rather than at import
# time so tests can vary it without needing a process restart.
_BROWSER_SEMAPHORE_LOCK = threading.Lock()
_BROWSER_SEMAPHORE: threading.Semaphore | None = None
_BROWSER_SEMAPHORE_SIZE: int | None = None


def _browser_semaphore(max_concurrent: int) -> threading.Semaphore:
    global _BROWSER_SEMAPHORE, _BROWSER_SEMAPHORE_SIZE  # noqa: PLW0603 - one process-wide pool, by design
    with _BROWSER_SEMAPHORE_LOCK:
        if _BROWSER_SEMAPHORE is None or _BROWSER_SEMAPHORE_SIZE != max_concurrent:
            _BROWSER_SEMAPHORE = threading.Semaphore(max(1, max_concurrent))
            _BROWSER_SEMAPHORE_SIZE = max_concurrent
        return _BROWSER_SEMAPHORE


# task-35 §3.4: which change types represent an ongoing condition that can
# self-heal (vs. a one-off fact like `title_changed`, which has nothing to
# "resolve" — the new title just IS the new title now).
_ROBOTS_TRACKED = frozenset({"robots_blocks_site", "robots_unavailable", "robots_missing"})
_PAGE_TRACKED = frozenset(
    {"page_noindex_appeared", "page_status_error", "gsc_verification_lost", "analytics_tag_lost"}
)
_SITEMAP_RESOURCE_TRACKED = frozenset({"sitemap_lost", "sitemap_unavailable"})
_SITEMAP_URL_TRACKED = frozenset(
    {"sitemap_url_redirects", "sitemap_url_broken", "sitemap_url_unavailable"}
)
_SITEMAP_AGGREGATE_TRACKED = frozenset({"sitemap_broken_urls"})
# `monitor_blocked` already existed in `app/monitor/severity.py` (WARNING)
# and in the frontend (`monitor-page.js` blockedClientIds/rowStateOf,
# `formatters.js` label, the `monitor-client-blocked.json` fixture) — task-36
# pre-wired the whole "Blocked" UI state ahead of this task, but nothing ever
# emitted the change type it was waiting for (task-50).
_SITE_PROTECTED_TRACKED = frozenset({"monitor_blocked"})
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
            related_id=change.related_id,
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


def resolve_base_url(
    client: Client, override: str | None, canonical_www: bool | None = None
) -> str:
    """`override` wins outright (a whole different URL — e.g. staging). Absent
    that, `canonical_www` (`MonitorSettings`, operator-set — task-38 follow-up)
    picks the right host directly instead of relying on the site's own
    redirect to correct it on every single request: `None` means "not known",
    so `client.domain` is used exactly as stored, same as before this field
    existed.

    Public (no leading underscore, task-51): the API and CLI "check redirects
    now" actions need the exact same host-resolution rule a scheduled run
    uses, not a second copy that could quietly drift from it.
    """
    if override:
        return override.rstrip("/")
    host = client.domain
    has_www = host.lower().startswith("www.")
    if canonical_www is True and not has_www:
        host = f"www.{host}"
    elif canonical_www is False and has_www:
        host = host[len("www.") :]
    return f"https://{host}".rstrip("/")


def effective_fetch_mode(monitor_settings: MonitorSettings | None, *, now: datetime) -> str:
    """`"http" | "browser" | "auto"` — what THIS run should use, resolving
    `MonitorSettings.fetch_mode` (task-50 §B4/§B5, revised task-58 §1.1/§1.3).

    `"http"`/`"browser"` are an explicit operator choice and win outright —
    an operator who forced `"http"` (the emergency kill switch, §B6) is never
    silently overridden. `"auto"` (the default) now passes straight through:
    the decision of whether any given PAGE needs the browser is made
    per-page, inside `run_client_monitor`'s page loop (`_should_escalate_to_
    browser`), not once for the whole run here.

    This used to also resolve `"auto"` to a whole-run `"browser"` for
    `sticky_days` after the last confirmed block (task-50 §B5) — that made
    sense when the ONLY way to escalate was "every page on this transport
    for the whole run" (task-50's fetch_mode was a per-run choice). task-58
    made escalation a per-page decision instead, remade fresh every single
    page every single run: a site that blocked at request #58 last time
    isn't dragged onto the slow lane for its other 795 pages, or for the
    next 14 days, on the strength of that one page. `now` is kept as a
    parameter for call-site stability even though this revision no longer
    reads a timestamp off `monitor_settings` — a future per-run signal is
    not being ruled out here, just none exists today.
    """
    if monitor_settings is None:
        return "auto"
    return monitor_settings.fetch_mode


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
                confirmed_gone=missing and fetched.status == 404,
                is_baseline_run=is_baseline_run,
            )
            changes = [dataclass_replace(c, related_id=resource.id) for c in changes]
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


# task-58 §1.2: which non-200 outcomes are worth a browser retry, uncondi-
# tionally — a classic bot-wall status, or a failure that could plausibly be
# OUR side of the connection rather than the site's.
_ALWAYS_ESCALATE_STATUSES = frozenset({403, 429, 503})


def _should_escalate_to_browser(result: FetchResult, *, previous_status: int | None) -> bool:
    """Whether a failed/suspicious HTTP fetch is worth retrying through
    Chromium (task-58 §1.2) — deliberately narrower than a blanket "any
    non-200": a `404`/`410` only escalates if the URL was genuinely `200`
    last time, never on a URL already confirmed dead. Without that carve-out
    a client whose sitemap still lists old, truly-gone URLs (live case:
    ALPHA's 39 post-Shopify-migration 404s) would pay a Chromium
    launch+navigation on every one of them, every single run, forever, for a
    fact already established. A final `3xx` never escalates — `fetch()`
    already follows redirects itself, so a 3xx reaching here is a redirect
    loop or cap, not something a browser resolves differently.
    """
    if result.error in ("blocked", "timeout", "connection_error"):
        return True
    status = result.status
    if status is None:
        return False  # e.g. `unsafe_url` — a browser will not fetch it either
    if status in _ALWAYS_ESCALATE_STATUSES or 500 <= status < 600:
        return True
    if status in (404, 410):
        return previous_status == 200
    return False


class _LazyBrowser:
    """One Chromium per page-worker THREAD (task-58 §1.3/§1.4) — launched on
    first actual need, reused for every subsequent page that SAME thread
    handles, and never shared with another thread: a Playwright `Page` is
    not thread-safe, so `monitor_page_workers` threads each need their own.
    Reuse across a thread's whole run is what keeps the ~0.5s launch cost
    (task-50 §0.7) from being paid once per fallback instead of once per
    worker.

    Bounded portfolio-wide by `semaphore`, acquired on first `get()` and
    released by `close()` — this is the "global semaphore on concurrent
    Chromium instances" task-58 §1.3 replaces the old dedicated antibot pool
    with: a slot is held for as long as this worker might still need
    Chromium, not per individual navigation.
    """

    def __init__(self, semaphore: threading.Semaphore) -> None:
        self._semaphore = semaphore
        self._session: BrowserFetchSession | None = None
        self._acquired = False

    def get(self) -> BrowserFetchSession:
        if self._session is None:
            self._semaphore.acquire()
            self._acquired = True
            try:
                self._session = BrowserFetchSession()
            except Exception:
                self._semaphore.release()
                self._acquired = False
                raise
        return self._session

    def close(self) -> None:
        try:
            if self._session is not None:
                self._session.close()
        finally:
            self._session = None
            if self._acquired:
                self._semaphore.release()
                self._acquired = False


class _FallbackBudget:
    """Caps how many pages one run may escalate to Chromium (task-58 §1.2)
    — a site that has gone down entirely must not turn into hundreds of
    Chromium launches chasing a fact `_should_escalate_to_browser` already
    knows on the first few: this is a blunt, cheap backstop underneath that
    smarter per-page logic, not a replacement for it. Shared, so it caps the
    whole run regardless of how many page workers are pulling from it.
    """

    def __init__(self, max_per_run: int) -> None:
        self._remaining = max_per_run
        self._lock = threading.Lock()

    def try_consume(self) -> bool:
        with self._lock:
            if self._remaining <= 0:
                return False
            self._remaining -= 1
            return True


@dataclass
class PageFallback:
    """Per-run HTTP→browser escalation config, handed to every page worker
    (task-58 §1.1). `lazy_browser` is intentionally per-CALLER (each page
    worker constructs and owns its own `_LazyBrowser`); `budget` and the
    browser throttle are shared across the whole run's workers.
    """

    browser_throttle: HostThrottle
    budget: _FallbackBudget


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
    browser_session: BrowserFetchSession | None = None,
    fallback: PageFallback | None = None,
    lazy_browser: _LazyBrowser | None = None,
) -> tuple[bool, str | None, bool, bool]:
    """Returns `(changed, status_kind, blocked, is_new)` — `status_kind` feeds
    the run's `urls_new/urls_changed/urls_gone` counters and the
    sitemap-health count; `blocked` tells the caller this fetch hit an
    antibot wall rather than observing anything real (task-50 §A2); `is_new`
    is whether this URL had no prior observation, straight from the same DB
    read `_persist_page_result` already has to do — the caller used to make
    its OWN redundant `repo.get_page(...)` call for exactly this, and that
    call ran on the shared `Session` **outside** `_COMMIT_LOCK` (task-58 §2.1:
    harmless under the old one-URL-at-a-time loop, a real race the moment
    more than one worker touches the session at once — SQLAlchemy's `Session`
    is not thread-safe).

    The network fetch AND all HTML parsing/fingerprinting happen before the
    lock below is acquired — none of it depends on database state, and
    `extract_signals`'s `html.parser` pass is real CPU, not free (task-58
    §0.4: ~41ms on a 780KB page, next to ~0.3ms for a commit). Only the
    fingerprint COMPARISON (against `prev_version`, a DB read) and the
    resulting writes happen inside `_COMMIT_LOCK`: `monitor_client_workers`
    threads — now also `monitor_page_workers` threads per client — hit the
    same SQLite file, which only ever allows one writer, so every DB touch —
    including a plain `session.flush()` inside a repository call, not just
    the periodic `_commit()` — must be serialized process-wide (task-35
    §3.6, live-measured 2026-08-11: unserialized writes under real 4-way
    concurrency produced "database is locked").

    Three ways this gets called (task-58 §1.1/§1.3):
    - `browser_session` given, `fallback=None` — this client's `fetch_mode`
      is the explicit `"browser"` override: every page goes straight to
      Chromium, no HTTP attempt at all (task-50 §B1 behavior, unchanged).
    - `fallback` given, `browser_session=None` — `fetch_mode="auto"`
      (the default): try the cheap HTTP lane first; escalate through
      `lazy_browser` only if `_should_escalate_to_browser` says so AND the
      run's `_FallbackBudget` still has room.
    - Neither given — `fetch_mode="http"` (the emergency kill switch, task-50
      §B6) or the single-page "Check now" path (`run_single_page_check`):
      plain HTTP, never escalates.
    """
    if browser_session is not None:
        result = fetch_via_browser(
            url,
            session=browser_session,
            throttle=throttle,
            stats=stats,
            timeout=settings.monitor_request_timeout_seconds,
            max_body_bytes=settings.monitor_max_body_bytes,
        )
    else:
        result = fetch(
            url,
            throttle=throttle,
            stats=stats,
            timeout=settings.monitor_request_timeout_seconds,
            max_body_bytes=settings.monitor_max_body_bytes,
        )
        if fallback is not None and lazy_browser is not None and result.status != 200:
            # A quick, LOCKED read of just the prior status — cheap (this is
            # not the removed §2.1 bug: that read decided a COUNTER off the
            # session outside any lock; this one decides which TRANSPORT to
            # use next, still fully inside `_COMMIT_LOCK`, and only runs for
            # the ~2-5% of pages that didn't already come back `200`.
            with _COMMIT_LOCK:
                previous = repo.get_page(client.id, url)
            previous_status = previous.status if previous is not None else None
            if (
                _should_escalate_to_browser(result, previous_status=previous_status)
                and fallback.budget.try_consume()
            ):
                result = fetch_via_browser(
                    url,
                    session=lazy_browser.get(),
                    throttle=fallback.browser_throttle,
                    stats=stats,
                    timeout=settings.monitor_request_timeout_seconds,
                    max_body_bytes=settings.monitor_max_body_bytes,
                )

    parsed = None if result.error == "blocked" else _parse_fetch_result(result)

    with _COMMIT_LOCK:
        return _persist_page_result(
            repo,
            client,
            url,
            result,
            parsed,
            run_id=run_id,
            is_key_page=is_key_page,
            in_sitemap=in_sitemap,
            settings=settings,
            artifacts_dir=artifacts_dir,
            is_baseline_run=is_baseline_run,
            now=now,
        )


@dataclass(frozen=True)
class _ParsedPage:
    """Everything `_persist_page_result` needs that is a pure function of
    `FetchResult` alone — computed once, outside `_COMMIT_LOCK` (task-58
    §2.5/§0.4): no field here ever depends on database state, so none of it
    belongs inside the lock that serializes the DB.
    """

    signals: pagemod.PageSignals
    x_robots_tag: str | None
    indexable: bool | None
    signals_dict: dict[str, Any]
    signals_sha256: str
    text_sha256: str | None
    raw_sha256: str | None


def _parse_fetch_result(result: FetchResult) -> _ParsedPage:
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
    return _ParsedPage(
        signals=signals,
        x_robots_tag=x_robots_tag,
        indexable=indexable,
        signals_dict=signals_dict,
        signals_sha256=fp.signals_fingerprint(signals_dict),
        text_sha256=fp.text_fingerprint(signals.visible_text) if result.body else None,
        raw_sha256=fp.raw_fingerprint(result.body) if result.body else None,
    )


def _persist_page_result(
    repo: MonitorRepository,
    client: Client,
    url: str,
    result: FetchResult,
    parsed: _ParsedPage | None,
    *,
    run_id: str,
    is_key_page: bool,
    in_sitemap: bool,
    settings: Settings,
    artifacts_dir: Path,
    is_baseline_run: bool,
    now: datetime,
) -> tuple[bool, str | None, bool, bool]:
    if result.error == "blocked":
        # An antibot interstitial is not a fact about the page (task-50
        # §A2/PLAN §12: absence of real data is never coerced into a value).
        # Do NOT create a page row for a URL never actually observed, do NOT
        # touch an existing page's state/`consecutive_failures`, and do NOT
        # write a version — the last genuine observation stays the truth
        # until a real fetch succeeds. The caller aggregates every blocked
        # URL this run into one `monitor_blocked` change instead of one row
        # per URL (task-50 §A3 — this is what LIVE 2026-08-19 wrote 304
        # individual `sitemap_url_broken`/`sitemap_url_unavailable` rows for).
        return False, None, True, False
    assert parsed is not None  # only `None` when `result.error == "blocked"`, handled above

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

    signals = parsed.signals
    x_robots_tag = parsed.x_robots_tag
    indexable = parsed.indexable
    signals_dict = parsed.signals_dict
    signals_sha256 = parsed.signals_sha256
    text_sha256 = parsed.text_sha256
    raw_sha256 = parsed.raw_sha256

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
    # `related_id` -> `page.id` so the change feed can link straight to this
    # page's diff viewer (`GET .../pages/{page_id}/diff`) instead of leaving
    # the operator staring at a bare "Page content changed" row with nothing
    # to click — the diff viewer already exists (task-36), it was just never
    # reachable from here.
    changes = [dataclass_replace(c, related_id=page.id) for c in changes]
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
        url_changes = [
            diffmod.DetectedChange(change_type, severity, "sitemap", url, related_id=page.id)
        ]
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
    return (is_new or is_changed), status_kind, False, is_new


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


class _TargetQueue:
    """Thread-safe puller over the ordered `targets` list (task-58 §1.4).

    Workers still pull in `targets`'s existing priority order (key pages,
    then open-issue pages, then oldest-checked-first — `pages_in_ring_
    order`) — parallelism only changes how many are IN FLIGHT at once, never
    the order they're offered in.
    """

    def __init__(self, targets: list[str]) -> None:
        self._targets = targets
        self._next_index = 0
        self._lock = threading.Lock()

    def next(self) -> str | None:
        with self._lock:
            if self._next_index >= len(self._targets):
                return None
            url = self._targets[self._next_index]
            self._next_index += 1
            return url

    def remaining(self) -> int:
        with self._lock:
            return len(self._targets) - self._next_index


class _PageLoopState:
    """Shared tallies across every page worker of one run (task-58 §2.2/
    §2.3) — plain local variables under the old one-URL-at-a-time loop, now
    genuinely touched by `monitor_page_workers` threads at once.

    Replaces `consecutive_blocked`/`monitor_block_streak_abort` (task-50
    §A4) with a sliding-window RATIO (`monitor_block_ratio_abort`):
    "consecutive" has no meaning once results arrive out of order from
    parallel workers, but "most of the last N attempts came back blocked"
    still says the same thing — the site (or its bot-mitigation) is not
    going to clear up mid-run, so stop paying for it.
    """

    def __init__(self, *, block_ratio_abort: float, window_size: int = 20) -> None:
        self._lock = threading.Lock()
        self.checked = 0
        self.changed_count = 0
        self.new_count = 0
        self.blocked_urls: list[str] = []
        self.truncated = False
        self.interrupted = False
        self._recent_blocked: deque[bool] = deque(maxlen=window_size)
        self._block_ratio_abort = block_ratio_abort
        self._stop = threading.Event()

    def record(self, *, url: str, blocked: bool, changed: bool, is_new: bool) -> bool:
        """Returns whether the CALLING worker should flush progress to the
        DB now — decided under the same lock as the increment, so exactly
        one thread ever sees `True` for a given multiple of ten, however
        many workers finish at the same instant."""
        with self._lock:
            self.checked += 1
            if blocked:
                self.blocked_urls.append(url)
            elif is_new:
                self.new_count += 1
            elif changed:
                self.changed_count += 1
            self._recent_blocked.append(blocked)
            if (
                len(self._recent_blocked) == self._recent_blocked.maxlen
                and sum(self._recent_blocked) / len(self._recent_blocked)
                >= self._block_ratio_abort
            ):
                self.truncated = True
                self._stop.set()
            return self.checked % 10 == 0

    def mark_truncated(self, *, interrupted: bool = False) -> None:
        with self._lock:
            self.truncated = True
            if interrupted:
                self.interrupted = True
        self._stop.set()

    def should_stop(self) -> bool:
        return self._stop.is_set()


def _run_page_worker(
    queue: _TargetQueue,
    state: _PageLoopState,
    *,
    repo: MonitorRepository,
    client: Client,
    run_id: str,
    key_pages: set[str],
    target_in_sitemap: dict[str, bool],
    throttle: HostThrottle,
    stats: FetchStats,
    settings: Settings,
    artifacts_dir: Path,
    is_baseline_run: bool,
    now: datetime,
    stop_event: threading.Event | None,
    deadline: datetime,
    fetch_mode: str,
    fallback: PageFallback | None,
    browser_semaphore: threading.Semaphore | None,
) -> None:
    """One `monitor_page_workers` slot for one client run (task-58 §1.4):
    pulls URLs off the shared `queue` until it is drained, `state` says to
    stop, the deadline passes, or `stop_event` fires. Owns exactly one
    `_LazyBrowser` for its whole lifetime here — never shared with another
    worker (task-58 §1.3: a Playwright `Page` is not thread-safe).
    """
    lazy_browser = _LazyBrowser(browser_semaphore) if browser_semaphore is not None else None
    try:
        while not state.should_stop():
            # "Зупинка: stop_event перевіряється між URL, тож SIGTERM не
            # чекає кінець обходу" (task-35 §3.6) — now per-worker instead of
            # once globally, same granularity as before.
            if stop_event is not None and stop_event.is_set():
                state.mark_truncated(interrupted=True)
                return
            if datetime.now(UTC) >= deadline:
                state.mark_truncated()
                return
            url = queue.next()
            if url is None:
                return
            browser_session = (
                lazy_browser.get() if fetch_mode == "browser" and lazy_browser is not None else None
            )
            changed, _status_kind, blocked, is_new_page = _process_page(
                repo,
                client,
                url,
                run_id=run_id,
                is_key_page=(url in key_pages),
                in_sitemap=target_in_sitemap.get(url, False),
                throttle=throttle,
                stats=stats,
                settings=settings,
                artifacts_dir=artifacts_dir,
                is_baseline_run=is_baseline_run,
                now=now,
                browser_session=browser_session,
                fallback=fallback,
                lazy_browser=lazy_browser,
            )
            should_flush = state.record(
                url=url, blocked=blocked, changed=changed, is_new=is_new_page
            )
            if should_flush:
                with _COMMIT_LOCK:
                    repo.update_run_counters(
                        run_id,
                        urls_checked=state.checked,
                        requests_made=stats.requests_made,
                        bytes_downloaded=stats.bytes_downloaded,
                    )
                    repo.commit()
    finally:
        if lazy_browser is not None:
            lazy_browser.close()


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
    fetch_mode: str = "http",
) -> RunOutcome:
    """`existing_run_id` claims an already-`queued` row (the API's "Check
    now" enqueues one; the scheduler claims and runs it — same split as
    `SyncJob`) instead of creating a fresh `running` row.

    `fetch_mode` ("http" | "browser" | "auto", task-50 §B, revised task-58
    §1.1/§1.3) is resolved by the CALLER (`effective_fetch_mode`, scheduler
    `_tick`) — this function just acts on it. `"browser"` sends every PAGE
    fetch straight to Chromium; `"http"` never touches Chromium at all
    (the emergency kill switch, unchanged from task-50 §B6); `"auto"` (the
    default) tries the cheap HTTP lane on every page first and escalates
    only the specific pages that need it (task-58's per-page fallback,
    `PageFallback`/`_should_escalate_to_browser`). robots.txt and sitemap
    fetches always stay on the cheap `urllib` lane regardless of
    `fetch_mode` (task-50 §B1: those succeed fine even on a client that
    later rate-limits the bulk of its page requests — live-measured on
    alphastyle.store, robots + sitemap all came back 200 before the 429
    wall hit around request #58).

    Pages themselves are checked by a pool of `settings.monitor_page_workers`
    threads (task-58 §1.4) pulling from one shared, priority-ordered queue —
    not one at a time. This is safe against the same SQLite-single-writer
    constraint that already governs `monitor_client_workers` (task-35 §3.6):
    every worker shares this ONE `session`/`repo`, and `_COMMIT_LOCK` is what
    makes that safe — every single touch of `session` from any worker thread,
    read or write, happens inside it, never outside (task-58 §2.1 is the
    postmortem for what happens when a read skips that rule).
    """
    now = now or datetime.now(UTC)
    repo = MonitorRepository(session)
    with _COMMIT_LOCK:
        monitor_settings = repo.get_or_create_settings(client.id)
        base_url = resolve_base_url(
            client, monitor_settings.base_url_override, monitor_settings.canonical_www
        )
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

        # Redirect monitoring (task-51) — cheap enough (one pull request, or
        # a bounded probe) to run on every light AND full pass, same as
        # robots/sitemap above; already committed internally.
        redirects_collect.process_redirects(
            repo,
            client,
            base_url,
            run_id=run.id,
            monitor_settings=monitor_settings,
            settings=settings,
            artifacts_dir=artifacts_dir,
            key_pages=key_pages,
            now=now,
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

        # task-58 §1.1/§1.3: transport is a per-page decision now, not a
        # per-run one — `page_throttle` allows `monitor_host_concurrency`
        # simultaneous requests to the client's own host (still one shared
        # `HostThrottle`, so all `monitor_page_workers` obey the same cap);
        # `browser_throttle` is the antibot lane's own, deliberately slower
        # pace (task-50 §B3 — 2.0s default vs. the plain lane's 0.25s),
        # shared by every worker's `_LazyBrowser` fallback fetches.
        page_throttle = HostThrottle(
            min_interval_seconds=min_interval, concurrency=settings.monitor_host_concurrency
        )
        browser_throttle = HostThrottle(
            min_interval_seconds=settings.monitor_antibot_min_interval_seconds
        )
        # `"browser"`/`"auto"` both need Chromium available (always, or as a
        # per-page fallback); `"http"` — the kill switch — never touches it,
        # so its pages never even try to acquire a slot from the shared cap.
        browser_semaphore = (
            _browser_semaphore(settings.monitor_browser_concurrency)
            if fetch_mode in ("browser", "auto")
            else None
        )
        fallback = (
            PageFallback(
                browser_throttle=browser_throttle,
                budget=_FallbackBudget(settings.monitor_browser_fallback_max_per_run),
            )
            if fetch_mode == "auto"
            else None
        )

        gone_count = 0
        queue = _TargetQueue(targets)
        state = _PageLoopState(block_ratio_abort=settings.monitor_block_ratio_abort)
        worker_count = max(1, settings.monitor_page_workers)
        with ThreadPoolExecutor(
            max_workers=worker_count, thread_name_prefix=f"monitor-page-{client.id[:8]}"
        ) as pool:
            futures = [
                pool.submit(
                    _run_page_worker,
                    queue,
                    state,
                    repo=repo,
                    client=client,
                    run_id=run.id,
                    key_pages=key_pages,
                    target_in_sitemap=target_in_sitemap,
                    throttle=page_throttle,
                    stats=stats,
                    settings=settings,
                    artifacts_dir=artifacts_dir,
                    is_baseline_run=is_baseline_run,
                    now=now,
                    stop_event=stop_event,
                    deadline=deadline,
                    fetch_mode=fetch_mode,
                    fallback=fallback,
                    browser_semaphore=browser_semaphore,
                )
                for _ in range(worker_count)
            ]
            for future in futures:
                future.result()  # re-raise a worker's exception, if any

        checked = state.checked
        changed_count = state.changed_count
        new_count = state.new_count
        blocked_urls = state.blocked_urls
        truncated = state.truncated
        interrupted = state.interrupted

        with _COMMIT_LOCK:
            # One aggregate change for the whole run, not one row per blocked
            # URL (task-50 §A3 — LIVE 2026-08-19 wrote 304 individual
            # `sitemap_url_*` rows for exactly this). Reuses `monitor_blocked`
            # (`app/monitor/severity.py`, WARNING) and its already-wired
            # frontend (task-36's `blockedClientIds`/`rowStateOf` on the
            # portfolio page, `formatters.js` label) rather than inventing a
            # new change type nothing renders yet. No baseline gate, same
            # reasoning as `_sitemap_aggregate` below: a client blocked from
            # its very first run is the single most important fact to
            # surface immediately, not noise to suppress.
            site_subject = f"{base_url}/"
            protection_changes: list[diffmod.DetectedChange] = []
            if blocked_urls:
                protection_changes = [
                    diffmod.DetectedChange(
                        "monitor_blocked",
                        sev.severity_of("monitor_blocked"),
                        "site",
                        site_subject,
                        new_value=f"{len(blocked_urls)} of {checked} checked this run",
                        details={
                            "blocked_count": len(blocked_urls),
                            "checked": checked,
                            "sample_urls": blocked_urls[:5],
                        },
                    )
                ]
                _apply_changes(repo, client.id, run.id, protection_changes, now=now)
                # `fetch_mode="auto"` clients stay on the antibot lane for
                # `monitor_antibot_sticky_days` after the last confirmed
                # block (task-50 §B5/`effective_fetch_mode`) — an explicit
                # operator choice (`"http"`/`"browser"`) is never touched
                # here (§B6).
                if monitor_settings.fetch_mode == "auto":
                    repo.update_settings(client.id, blocked_detected_at=now)
            _reconcile(
                repo,
                client.id,
                run.id,
                site_subject,
                tracked_types=_SITE_PROTECTED_TRACKED,
                detected=protection_changes,
                now=now,
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

        # Honest run status (task-50 §A5): `RunOutcome`/`finish_run` used to
        # both hardcode "succeeded" even when `interrupted` was true — this
        # keeps them in agreement instead of the scheduler's own log line
        # disagreeing with what got written to the run row.
        blocked_count = len(blocked_urls)
        if interrupted:
            final_status = "failed_interrupted"
            error_code = None
        elif blocked_count and checked and blocked_count >= checked:
            # Every single page attempt this run hit the wall — nothing real
            # was gathered, so this is a failure, not a quiet "succeeded"
            # with zero real signal (task-50 §A5).
            final_status = "failed"
            error_code = "blocked_by_protection"
        else:
            final_status = "succeeded"
            error_code = "partially_blocked" if blocked_count else None

        # Always present (even at 0) for every run through this code path —
        # `None` (the API DTO's absence case) is reserved for runs written
        # before this field existed, never for "we checked and found no
        # blocks" (PLAN §12 invariant, task-50 §4).
        notes: dict[str, Any] = {"baseline": is_baseline_run, "blocked_count": blocked_count}

        with _COMMIT_LOCK:
            # What THIS run found, not what is currently open client-wide
            # (task-37 F4) — a light run on 1 URL must not report the same
            # numbers as a full run on 609.
            counts_by_severity = repo.count_changes_by_severity(run.id)
            remaining = len(targets) - checked if truncated else None
            repo.finish_run(
                run.id,
                status=final_status,
                truncated=truncated,
                remaining_count=remaining,
                error_code=error_code,
                notes=notes,
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
            run_id=run.id, status=final_status, urls_checked=checked, truncated=truncated
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


def run_single_page_check(
    session: Session,
    client: Client,
    page: MonitorPage,
    *,
    settings: Settings,
    artifacts_dir: Path,
    now: datetime | None = None,
) -> MonitorPage:
    """Re-check one URL on demand (task-38 §2) — an operator who just fixed a
    `noindex` on one page should not have to wait for the next scheduled
    sweep or run all 600+ URLs to confirm it.

    Deliberately thin: reuses `_process_page` exactly as `run_client_monitor`
    does, so a page checked this way gets the same fetch politeness
    (`HostThrottle`), fingerprinting, versioning and diffing as any other —
    the fix shows up as a real, diffable, alertable change, not a shortcut
    that bypasses history. `is_baseline_run=False` unconditionally: this path
    only ever targets a page that already exists in `monitor_pages` (picked
    from the Pages table), so it is by definition not the client's first-ever
    look at it.

    Raises `PageCheckConflict` if a check for this exact page is already in
    flight — never fires a second concurrent fetch at the client's site.
    """
    now = now or datetime.now(UTC)
    with _PAGE_CHECK_GUARD:
        if page.id in _PAGE_CHECK_IN_FLIGHT:
            raise PageCheckConflict(page.id)
        _PAGE_CHECK_IN_FLIGHT.add(page.id)
    try:
        repo = MonitorRepository(session)
        with _COMMIT_LOCK:
            run = repo.create_run(client.id, kind=PAGE, trigger="manual")
            session.commit()

        min_interval = crawl_delay_floor(None, settings.monitor_host_min_interval_seconds)
        throttle = HostThrottle(min_interval_seconds=min_interval)
        stats = FetchStats()

        try:
            _process_page(
                repo,
                client,
                page.url,
                run_id=run.id,
                is_key_page=page.is_key_page,
                in_sitemap=page.in_sitemap,
                throttle=throttle,
                stats=stats,
                settings=settings,
                artifacts_dir=artifacts_dir,
                is_baseline_run=False,
                now=now,
            )
        except Exception as exc:  # noqa: BLE001 - the run row must reflect the failure either way
            logger.exception(
                "monitor_page_check_failed client_id=%s page_id=%s", client.id, page.id
            )
            with _COMMIT_LOCK:
                repo.finish_run(
                    run.id, status="failed", error_code="run_failed", error_message=str(exc)[:900]
                )
                session.commit()
            raise

        with _COMMIT_LOCK:
            counts_by_severity = repo.count_changes_by_severity(run.id)
            repo.finish_run(run.id, status="succeeded")
            repo.update_run_counters(
                run.id,
                urls_total=1,
                urls_checked=1,
                requests_made=stats.requests_made,
                bytes_downloaded=stats.bytes_downloaded,
                changes_critical=counts_by_severity.get("critical", 0),
                changes_warning=counts_by_severity.get("warning", 0),
                changes_info=counts_by_severity.get("info", 0),
            )
            session.commit()

        updated = repo.get_page_by_id(page.id)
        assert updated is not None, "the page this function was called with must still exist"
        return updated
    finally:
        with _PAGE_CHECK_GUARD:
            _PAGE_CHECK_IN_FLIGHT.discard(page.id)


def check_robots_now(
    session: Session,
    client: Client,
    *,
    settings: Settings,
    artifacts_dir: Path,
    now: datetime | None = None,
) -> None:
    """Re-check robots.txt on demand (mirrors `run_single_page_check`,
    task-38 §2) — an operator who just fixed a `Disallow` should not have to
    wait for the next scheduled sweep to confirm it. `is_baseline_run=False`
    unconditionally: this path only ever runs for a client that already has
    at least one finished run (the "Robots" block only renders once data
    exists), so it is never the client's first-ever look at the file.
    """
    now = now or datetime.now(UTC)
    repo = MonitorRepository(session)
    with _COMMIT_LOCK:
        run = repo.create_run(client.id, kind=LIGHT, trigger="manual")
        session.commit()
    monitor_settings = repo.get_or_create_settings(client.id)
    base_url = resolve_base_url(
        client, monitor_settings.base_url_override, monitor_settings.canonical_www
    )
    min_interval = crawl_delay_floor(None, settings.monitor_host_min_interval_seconds)
    throttle = HostThrottle(min_interval_seconds=min_interval)
    stats = FetchStats()
    try:
        _process_robots(
            repo,
            client,
            base_url,
            run_id=run.id,
            throttle=throttle,
            stats=stats,
            settings=settings,
            artifacts_dir=artifacts_dir,
            is_baseline_run=False,
            now=now,
        )
    except Exception as exc:  # noqa: BLE001 - the run row must reflect the failure either way
        logger.exception("monitor_robots_check_failed client_id=%s", client.id)
        with _COMMIT_LOCK:
            repo.finish_run(
                run.id, status="failed", error_code="run_failed", error_message=str(exc)[:900]
            )
            session.commit()
        raise
    with _COMMIT_LOCK:
        repo.finish_run(run.id, status="succeeded")
        session.commit()


def check_sitemap_now(
    session: Session,
    client: Client,
    *,
    settings: Settings,
    artifacts_dir: Path,
    now: datetime | None = None,
) -> None:
    """Re-check the sitemap tree on demand — same rationale as
    `check_robots_now`. Reads the last-fetched robots.txt from storage
    (rather than re-fetching it) purely to find declared `Sitemap:` entries;
    a missing/never-fetched robots resource falls back to the same
    `{base_url}/sitemap.xml` default `_process_sitemaps` uses.
    """
    now = now or datetime.now(UTC)
    repo = MonitorRepository(session)
    with _COMMIT_LOCK:
        run = repo.create_run(client.id, kind=LIGHT, trigger="manual")
        session.commit()
    monitor_settings = repo.get_or_create_settings(client.id)
    base_url = resolve_base_url(
        client, monitor_settings.base_url_override, monitor_settings.canonical_www
    )
    robots_resource = repo.get_resource(client.id, f"{base_url}/robots.txt")
    robots_doc = (
        robotsmod.from_structure(robots_resource.structure_json)
        if robots_resource and robots_resource.structure_json
        else robotsmod.RobotsDocument()
    )
    min_interval = crawl_delay_floor(None, settings.monitor_host_min_interval_seconds)
    throttle = HostThrottle(min_interval_seconds=min_interval)
    stats = FetchStats()
    try:
        _process_sitemaps(
            repo,
            client,
            base_url,
            robots_doc,
            run_id=run.id,
            throttle=throttle,
            stats=stats,
            settings=settings,
            artifacts_dir=artifacts_dir,
            is_baseline_run=False,
            now=now,
        )
    except Exception as exc:  # noqa: BLE001 - the run row must reflect the failure either way
        logger.exception("monitor_sitemap_check_failed client_id=%s", client.id)
        with _COMMIT_LOCK:
            repo.finish_run(
                run.id, status="failed", error_code="run_failed", error_message=str(exc)[:900]
            )
            session.commit()
        raise
    with _COMMIT_LOCK:
        repo.finish_run(run.id, status="succeeded")
        session.commit()
