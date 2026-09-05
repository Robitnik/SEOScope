"""Web Archive capture pipeline (task-59 §3): one client's capture pass.

Mirrors `app.monitor.runner.run_client_monitor`'s isolation contract (PLAN
§9.1: the cell — here `(client, url)` — is atomic; one URL's exception never
takes down the rest of the pass) but is an entirely independent module —
nothing here calls into, or is called by, `app/monitor/**`.

Sequential within one client, on purpose, for this first cut: the shared
per-host mutex (`HostThrottle(concurrency=archive_host_concurrency)`,
default 1 until task-58 lands) already serializes same-host requests
regardless of how many threads submit them, so a thread pool here would add
real complexity (session thread-safety) for no measured throughput gain
right now. Portfolio-wide parallelism instead comes from
`ArchiveScheduler` running multiple clients' captures concurrently, each
with its own session — the same split Site Monitor's own scheduler uses
(PLAN §9.1: "between services parallel, within one sequential").
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.models import ArchiveAssetVersion, ArchiveUrl, Client, MonitorPage
from app.db.repositories.web_archive import WebArchiveRepository
from app.monitor.fetcher import FetchStats, fetch
from app.monitor.page import decode_body
from app.tools.http_safety import HostThrottle
from app.webarchive import assets as assetsmod
from app.webarchive import changes as changesmod
from app.webarchive import storage
from app.webarchive.db_retry import commit_with_retry
from app.webarchive.fingerprint import content_fingerprint
from app.webarchive.urls import is_asset_like_url, looks_like_binary_content_type, normalize_url
from app.webarchive.writequeue import ArchiveWriteController

logger = logging.getLogger(__name__)

# `Set-Cookie` never reaches storage (PLAN §13: no secrets in stored data or
# events) — a session cookie is exactly the kind of value that must never
# end up replayed back to an operator's browser from an archived response.
_STRIP_RESPONSE_HEADERS = frozenset({"set-cookie"})

# How often the capture's own progress counters reach the database, so
# the hub's Recent captures panel can animate a real fraction instead of
# a spinner with no numbers behind it.
_COUNTER_FLUSH_EVERY_URLS = 20
_COUNTER_FLUSH_SECONDS = 5.0

# URLs inserted per transaction while seeding a client's frontier, and the
# lock that keeps two clients from seeding at the same time.
_SEED_COMMIT_BATCH = 100
_SEED_LOCK = threading.Lock()

# A capture pass with more than this fraction of its URLs coming back
# `blocked` is reported as `blocked`, not `partial` — mirrors
# `monitor_block_ratio_abort`'s reasoning (task-50): a site-wide antibot
# wall is a different, more important signal than "a few pages failed".
_BLOCKED_RATIO_THRESHOLD = 0.8


@dataclass
class CaptureOutcome:
    status: str  # "ok" | "partial" | "failed" | "blocked"
    urls_total: int
    urls_captured: int = 0
    urls_new: int = 0
    urls_unchanged: int = 0
    urls_failed: int = 0
    urls_blocked: int = 0
    assets_fetched: int = 0
    assets_reused: int = 0
    assets_failed: int = 0
    bytes_stored: int = 0
    error_code: str | None = None
    error_message: str | None = None


@dataclass
class _AssetCaptureOutcome:
    links: list[tuple[str, str | None, str]] = field(default_factory=list)
    fetched: int = 0
    reused: int = 0
    failed: int = 0
    truncated: bool = False


@dataclass
class UrlCaptureResult:
    outcome: str  # "new" | "unchanged" | "failed" | "blocked"
    bytes_stored: int = 0
    assets_fetched: int = 0
    assets_reused: int = 0
    assets_failed: int = 0


@dataclass
class BackfillOutcome:
    """Result of `backfill_css_assets` (task-60 §1.3) — a retroactive sweep
    for CSS-only references that predate the discovery fix above, or that
    slipped past it (a CSS file whose own body changed after capture).
    """
    dry_run: bool
    scanned_snapshots: int = 0
    scanned_css_files: int = 0
    missing_by_kind: dict[str, int] = field(default_factory=dict)
    fetched: int = 0
    failed: int = 0

    @property
    def missing_total(self) -> int:
        return sum(self.missing_by_kind.values())


def _strip_headers(headers: dict[str, str]) -> dict[str, str]:
    return {
        key: value for key, value in headers.items()
        if key.lower() not in _STRIP_RESPONSE_HEADERS
    }


def seed_urls_from_monitor(
    session: Session,
    repo: WebArchiveRepository,
    client: Client,
    *,
    max_urls: int,
    write_controller: ArchiveWriteController,
) -> int:
    """Every `monitor_pages` row this client already has becomes a tracked
    `ArchiveUrl` (`source="sitemap"`) if it isn't one yet — the monitor's
    own sitemap discovery IS the archive's URL seed list (task-59 §1.9),
    so no separate discovery crawl is needed. Idempotent: `get_or_create_url`
    no-ops for a URL already tracked.
    """
    stmt = select(MonitorPage.url).where(MonitorPage.client_id == client.id).limit(max_urls)
    urls = [url for (url,) in session.execute(stmt).all()]

    def _seed_batch(batch: list[str]) -> int:
        seeded = 0
        for url in batch:
            key = normalize_url(url)
            if repo.find_url(client.id, key) is None:
                seeded += 1
            repo.get_or_create_url(client.id, url, key, source="sitemap")
        return seeded

    # Batched AND retried as a whole batch, with the seed phase serialized
    # process-wide (`_SEED_LOCK`). All three matter, for the same live
    # incident (task-59, 2026-09-04): seeding a client inserts hundreds of
    # rows (ALPHA: 794), six clients bootstrap in the same minute, and
    # SQLite has exactly one writer. One transaction around the whole seed
    # held the writer lock for the entire burst; retrying only the COMMIT
    # did not help either, because `get_or_create_url` flushes per row and
    # it was those flushes that raised `database is locked`. Re-running a
    # batch is safe — `get_or_create_url` is idempotent by `url_key`.
    added = 0
    with _SEED_LOCK:
        for start in range(0, len(urls), _SEED_COMMIT_BATCH):
            batch = urls[start : start + _SEED_COMMIT_BATCH]
            added += write_controller.submit(session, partial(_seed_batch, batch))
    return added


def _check_or_fetch_asset(
    repo: WebArchiveRepository,
    asset: Any,
    *,
    settings: Settings,
    artifacts_dir: Path,
    throttle: HostThrottle,
    stats: FetchStats,
    now: datetime,
) -> tuple[ArchiveAssetVersion | None, str]:
    """`(version, outcome)`, outcome in `{"fetched", "revalidated", "failed"}`.

    "revalidated" covers BOTH a real `304` (live-measured 10/10 on client
    sites, task-59 §0.5) and a full `200` whose body happens to hash to the
    version already on file — a site with no validators at all still gets
    the same "no new blob written" benefit, just at the cost of a full
    re-download instead of a cheap 304. `asset.etag`/`last_modified` are
    updated here from whatever the response actually carried, so the NEXT
    revalidation always uses the freshest validator the site sent.
    """
    extra_headers: dict[str, str] = {}
    if asset.etag:
        extra_headers["If-None-Match"] = asset.etag
    elif asset.last_modified:
        extra_headers["If-Modified-Since"] = asset.last_modified

    result = fetch(
        asset.url,
        throttle=throttle,
        stats=stats,
        timeout=settings.archive_request_timeout_seconds,
        max_body_bytes=settings.archive_asset_max_bytes,
        extra_headers=extra_headers or None,
    )
    prior_version = repo.latest_asset_version(asset.id)
    new_etag = result.headers.get("ETag") if result.headers else None
    new_last_modified = result.headers.get("Last-Modified") if result.headers else None

    if result.status == 304 and prior_version is not None:
        repo.confirm_asset_version(prior_version, at=now)
        repo.record_asset_check(
            asset, checked_at=now,
            etag=new_etag or asset.etag, last_modified=new_last_modified or asset.last_modified,
            failed=False,
        )
        return prior_version, "revalidated"
    if not result.ok or result.body is None:
        repo.record_asset_check(
            asset, checked_at=now, etag=asset.etag, last_modified=asset.last_modified, failed=True
        )
        return prior_version, "failed"

    blob = storage.store_blob(artifacts_dir, result.body)
    repo.register_blob(
        blob.sha256, size_bytes=blob.size_bytes, gzip_size_bytes=blob.gzip_size_bytes,
        content_type=result.headers.get("Content-Type"), path=blob.path,
    )
    repo.record_asset_check(
        asset, checked_at=now, etag=new_etag, last_modified=new_last_modified, failed=False
    )
    if prior_version is not None and prior_version.blob_sha256 == blob.sha256:
        repo.confirm_asset_version(prior_version, at=now)
        return prior_version, "revalidated"

    version = repo.create_asset_version(
        asset_id=asset.id,
        blob_sha256=blob.sha256,
        content_type=result.headers.get("Content-Type"),
        http_status=result.status,
        first_seen_at=now,
        last_confirmed_at=now,
    )
    return version, "fetched"


def _read_asset_text(artifacts_dir: Path, version: ArchiveAssetVersion | None) -> str | None:
    """The stored body of an already-fetched CSS or JS asset version,
    decoded for a second regex pass (task-60 §1.2 for CSS, task-63 §3.3 for
    JS) — `None` for anything not actually on disk (a truncated/failed
    asset has no blob at all).
    """
    if version is None or not version.blob_sha256:
        return None
    try:
        raw = storage.read_blob(artifacts_dir, version.blob_sha256)
    except OSError:
        return None
    return raw.decode("utf-8", errors="replace")


def _capture_assets_for_page(
    session: Session,
    repo: WebArchiveRepository,
    html_text: str,
    *,
    base_url: str,
    settings: Settings,
    artifacts_dir: Path,
    throttle: HostThrottle,
    stats: FetchStats,
    now: datetime,
    capture_id: str,
    write_controller: ArchiveWriteController,
) -> _AssetCaptureOutcome:
    outcome = _AssetCaptureOutcome()
    remaining_new_budget = settings.archive_new_assets_per_page

    # A queue, not a flat loop (task-60 §1.2): once a `kind == "css"` asset's
    # body is on disk, it is scanned for its OWN `url(...)` references
    # (backgrounds, `@font-face` sources) and anything new is enqueued —
    # this is the fix for the CSS-only asset gap (task-60 §0.1: a real
    # background-image was referenced ONLY from a fetched CSS file's body
    # and so was never discovered at all). One level deep ONLY: a ref
    # discovered THIS way is always tagged "img"/"font" by
    # `discover_css_assets`, never "css", so it is never itself expanded —
    # matching the measured, accepted limit (task-60 §0.4: one `@import`
    # across the whole portfolio).
    queue: deque[assetsmod.AssetRef] = deque(assetsmod.discover_html_assets(html_text, base_url))
    seen_keys: set[str] = {normalize_url(ref.url) for ref in queue}
    css_expanded: set[str] = set()
    js_expanded: set[str] = set()
    # Shared, page-wide budget (task-63 §3.3) — not per-script — so one
    # heavy minified bundle cannot alone exhaust it before the page's other
    # scripts get a turn.
    js_discovery_budget = settings.archive_js_discovered_assets_per_page

    def _enqueue_js_string_assets(js_text: str, js_base_url: str) -> None:
        nonlocal js_discovery_budget
        if js_discovery_budget <= 0:
            return
        for nested in assetsmod.discover_js_string_assets(
            js_text, js_base_url, limit=js_discovery_budget
        ):
            nested_key = normalize_url(nested.url)
            if nested_key in seen_keys:
                continue
            seen_keys.add(nested_key)
            queue.append(nested)
            js_discovery_budget -= 1
            if js_discovery_budget <= 0:
                break

    # Inline `<script>` bodies live in the PAGE's own HTML, not in any
    # fetched asset — scanned once, up front, same as `discover_html_assets`
    # already does for inline `<style>` bodies.
    for inline_body in assetsmod.inline_script_bodies(html_text):
        _enqueue_js_string_assets(inline_body, base_url)

    while queue:
        ref = queue.popleft()
        key = normalize_url(ref.url)
        asset = repo.get_or_create_asset(ref.url, key, kind=ref.kind)
        last_checked_at = asset.last_checked_at
        never_checked = last_checked_at is None
        needs_check = never_checked or (
            last_checked_at is not None
            and now - last_checked_at >= timedelta(days=settings.archive_asset_revalidate_days)
        )

        version: ArchiveAssetVersion | None
        if not needs_check:
            version = repo.latest_asset_version(asset.id)
            outcome.links.append((asset.id, version.id if version else None, ref.kind))
            outcome.reused += 1
        elif never_checked and remaining_new_budget <= 0:
            outcome.truncated = True
            outcome.links.append((asset.id, None, ref.kind))
            outcome.failed += 1
            version = None
        else:
            try:
                version, result_kind = _check_or_fetch_asset(
                    repo, asset, settings=settings, artifacts_dir=artifacts_dir,
                    throttle=throttle, stats=stats, now=now,
                )
            except Exception:  # noqa: BLE001 - one bad asset must never fail the whole page
                # Live incident (task-59, 2026-09-04): a real filename containing
                # a bare en-dash crashed `urllib`'s ASCII-only request line
                # before `app.webarchive.urls._ascii_safe` existed — that one
                # asset took the ENTIRE page down with it, which PLAN §9.1's
                # cell-isolation principle applies here one level deeper than
                # just "one URL": one asset is its own cell within the page.
                logger.warning("archive_asset_crashed asset_url=%s", asset.url, exc_info=True)
                version, result_kind = None, "failed"
            # Commit per asset, and this is the single most important write in
            # the pipeline for portfolio-wide health: SQLite takes its one
            # writer lock at the FIRST write of a transaction and holds it until
            # commit, so batching a page's assets into one transaction pins that
            # lock across every asset's NETWORK round-trip — a 200-asset page
            # held it for minutes. Live incident (task-59, 2026-09-04): with six
            # clients capturing at once that starved everything else past
            # SQLite's 30s `busy_timeout`, and the resulting
            # `database is locked` crashes are what left seven captures wedged
            # at "in progress…". One asset per transaction keeps the lock held
            # for microseconds of local writes instead — routed through
            # `write_controller` (task-62 §2.2) so it never even contends with
            # another capture's own commit.
            #
            # Piggybacks a `heartbeat_at` touch on this SAME commit (task-62
            # §2.3): a page with a p90 of 476 subresources (task-59 §0.4) can
            # spend minutes inside THIS loop between two outer per-URL flushes
            # — without this, `_reclaim_dead_captures`'s heartbeat-staleness
            # check could wrongly kill a capture that is still working, just
            # slowly, on one asset-heavy page.
            write_controller.submit(
                session,
                partial(
                    repo.update_capture_counters, capture_id, heartbeat_at=datetime.now(UTC)
                ),
            )
            outcome.links.append((asset.id, version.id if version else None, ref.kind))
            if result_kind == "fetched":
                outcome.fetched += 1
                if never_checked:
                    remaining_new_budget -= 1
            elif result_kind == "revalidated":
                outcome.reused += 1
            else:
                outcome.failed += 1

        if ref.kind == "css" and key not in css_expanded and version is not None:
            css_expanded.add(key)
            css_text = _read_asset_text(artifacts_dir, version)
            if css_text:
                for nested in assetsmod.discover_css_assets(css_text, ref.url):
                    nested_key = normalize_url(nested.url)
                    if nested_key not in seen_keys:
                        seen_keys.add(nested_key)
                        queue.append(nested)
        elif ref.kind == "js" and key not in js_expanded and version is not None:
            js_expanded.add(key)
            js_text = _read_asset_text(artifacts_dir, version)
            if js_text:
                _enqueue_js_string_assets(js_text, ref.url)

    return outcome


def _make_cancel_checker(
    session_scope: Callable[[], AbstractContextManager[Session]], capture_id: str
) -> Callable[[], bool]:
    """Same shape as `app.jobs.runner._make_cancel_checker`: a brand-new,
    short-lived session per check, not the capture's own long-lived one —
    that session's identity map holds `capture_id`'s row from way back at
    `update_capture_counters`'s first call, and this project's sessionmaker
    is `expire_on_commit=False` (`app/db/session.py`), so re-reading it on
    the SAME session would keep returning the stale cached object and never
    observe `cancel_requested` being set by a different session (the API
    request that handled "Cancel").
    """

    def _check() -> bool:
        try:
            with session_scope() as session:
                capture = WebArchiveRepository(session).get_capture(capture_id)
                return capture is not None and bool(capture.cancel_requested)
        except Exception:  # noqa: BLE001 - a failed poll must never abort the capture
            return False

    return _check


def _resolve_reason(
    *, is_first: bool, content_changed: bool, fingerprint_same: bool
) -> str:
    if is_first:
        return "first"
    if content_changed:
        return "status_changed" if fingerprint_same else "changed"
    return "weekly_refresh"


def capture_url(
    session: Session,
    repo: WebArchiveRepository,
    client: Client,
    archive_url: ArchiveUrl,
    *,
    capture_id: str,
    settings: Settings,
    artifacts_dir: Path,
    throttle: HostThrottle,
    stats: FetchStats,
    now: datetime,
    capture_assets: bool,
    write_controller: ArchiveWriteController,
) -> UrlCaptureResult:
    """One URL, one cell (PLAN §9.1). The caller tallies the result into the
    capture's own counters and NEVER lets one raise stop the pass (see
    `run_client_capture`).
    """
    result = fetch(
        archive_url.url,
        throttle=throttle,
        stats=stats,
        timeout=settings.archive_request_timeout_seconds,
        max_body_bytes=settings.archive_html_max_bytes,
    )
    if result.error == "blocked":
        return UrlCaptureResult(outcome="blocked")
    if result.error is not None or result.body is None:
        if result.status == 404:
            repo.mark_url_gone(archive_url, observed_at=now)
        return UrlCaptureResult(outcome="failed")

    content_type_header = result.headers.get("Content-Type")
    if content_type_header and looks_like_binary_content_type(content_type_header):
        # This ArchiveUrl was never really a page — a same-host link
        # pointing straight at an image/font/media file (task-59,
        # 2026-09-04 live incident, see `app.webarchive.urls`'s module
        # note) got queued into the page frontier before
        # `is_asset_like_url` existed. Drop it rather than storing a
        # garbled "page" snapshot of raw bytes forever: the real resource
        # is already tracked correctly via `archive_assets` (discovered
        # independently off the same page's `<img>`/`srcset`), and a
        # page-shaped row for the SAME url would otherwise shadow that
        # asset at replay time (`_resolve_replay` checks pages first).
        repo.delete_url(archive_url)
        return UrlCaptureResult(outcome="failed")

    html_text = decode_body(result.body, content_type=content_type_header)
    fingerprint = content_fingerprint(html_text)

    prev_snapshot = repo.latest_snapshot(archive_url.id)
    is_first = prev_snapshot is None
    if prev_snapshot is None:
        content_changed = True
        is_stale = False
    else:
        content_changed = (
            fingerprint != prev_snapshot.content_fingerprint
            or result.status != prev_snapshot.http_status
            or (result.final_url or None) != (prev_snapshot.final_url or None)
        )
        is_stale = now - prev_snapshot.observed_at >= timedelta(
            days=settings.archive_refresh_after_days
        )

    if not content_changed and not is_stale:
        assert prev_snapshot is not None
        repo.confirm_snapshot(prev_snapshot, observed_at=now)
        repo.touch_url(archive_url, observed_at=now, snapshot_written=False)
        return UrlCaptureResult(outcome="unchanged")

    fingerprint_same = (
        prev_snapshot is not None and fingerprint == prev_snapshot.content_fingerprint
    )
    store_reason = _resolve_reason(
        is_first=is_first, content_changed=content_changed, fingerprint_same=fingerprint_same
    )

    blob = storage.store_blob(artifacts_dir, result.body)
    repo.register_blob(
        blob.sha256, size_bytes=blob.size_bytes, gzip_size_bytes=blob.gzip_size_bytes,
        content_type=result.headers.get("Content-Type"), path=blob.path,
    )

    asset_outcome = _AssetCaptureOutcome()
    if capture_assets:
        asset_outcome = _capture_assets_for_page(
            session, repo, html_text, base_url=result.final_url, settings=settings,
            artifacts_dir=artifacts_dir, throttle=throttle, stats=stats, now=now,
            capture_id=capture_id, write_controller=write_controller,
        )

    new_fields = changesmod.snapshot_fields(
        html_text, base_url=result.final_url, http_status=result.status, final_url=result.final_url
    )

    snapshot = repo.create_snapshot(
        archive_url_id=archive_url.id,
        client_id=client.id,
        capture_id=capture_id,
        observed_at=now,
        http_status=result.status,
        final_url=result.final_url,
        headers_json=_strip_headers(result.headers),
        content_type=result.headers.get("Content-Type"),
        content_fingerprint=fingerprint,
        raw_sha256=blob.sha256,
        body_blob_sha256=blob.sha256,
        title=new_fields.title,
        description=new_fields.description,
        canonical=new_fields.canonical,
        meta_robots=new_fields.meta_robots,
        word_count=len(new_fields.visible_text.split()),
        asset_count=len(asset_outcome.links),
        assets_truncated=asset_outcome.truncated,
        store_reason=store_reason,
        prev_snapshot_id=prev_snapshot.id if prev_snapshot else None,
        last_confirmed_at=now,
        confirmed_count=1,
    )

    for asset_id, asset_version_id, role in asset_outcome.links:
        repo.link_snapshot_asset(
            snapshot_id=snapshot.id, asset_id=asset_id, asset_version_id=asset_version_id, role=role
        )

    if not is_first and content_changed:
        assert prev_snapshot is not None and prev_snapshot.body_blob_sha256
        old_html_bytes = storage.read_blob(artifacts_dir, prev_snapshot.body_blob_sha256)
        old_html_text = decode_body(old_html_bytes, content_type=prev_snapshot.content_type)
        old_fields = changesmod.snapshot_fields(
            old_html_text, base_url=prev_snapshot.final_url or archive_url.url,
            http_status=prev_snapshot.http_status, final_url=prev_snapshot.final_url,
        )
        diff = changesmod.diff_fields(old_fields, new_fields)
        if diff is not None:
            repo.create_change(
                client_id=client.id,
                archive_url_id=archive_url.id,
                snapshot_id=snapshot.id,
                prev_snapshot_id=prev_snapshot.id,
                change_kinds_json=diff.change_kinds,
                summary_json=diff.summary,
                text_delta_words=diff.text_delta_words,
            )

    repo.touch_url(archive_url, observed_at=now, snapshot_written=True)
    _discover_same_host_links(repo, client, archive_url, new_fields.links)
    return UrlCaptureResult(
        outcome="new",
        bytes_stored=blob.gzip_size_bytes,
        assets_fetched=asset_outcome.fetched,
        assets_reused=asset_outcome.reused,
        assets_failed=asset_outcome.failed,
    )


def _discover_same_host_links(
    repo: WebArchiveRepository, client: Client, archive_url: ArchiveUrl, links: frozenset[str]
) -> None:
    """Same-host links found while parsing an already-fetched page become
    tracked URLs (`source="link"`) for the NEXT capture pass (task-59 §1.9)
    — this is what catches a real page the sitemap never listed. Off-host
    links (CDNs, social, the client's OWN other domains) are deliberately
    never followed here; the monitor's own sitemap seed
    (`seed_urls_from_monitor`) is the only OTHER source of new URLs.
    """
    page_host = urlsplit(archive_url.url).hostname
    if not page_host:
        return
    for link in links:
        if urlsplit(link).hostname != page_host:
            continue
        if is_asset_like_url(link):
            # A gallery lightbox's `<a href=".../photo.webp">` is a same-
            # host link but not a page a visitor lands on — see
            # `app.webarchive.urls`'s module note (task-59, 2026-09-04).
            continue
        key = normalize_url(link)
        if repo.find_url(client.id, key) is None:
            repo.get_or_create_url(client.id, link, key, source="link")


def _capture_url_and_commit(
    session: Session,
    repo: WebArchiveRepository,
    client: Client,
    archive_url: ArchiveUrl,
    *,
    capture_id: str,
    settings: Settings,
    artifacts_dir: Path,
    throttle: HostThrottle,
    stats: FetchStats,
    now: datetime,
    capture_assets: bool,
    write_controller: ArchiveWriteController,
) -> UrlCaptureResult:
    """`capture_url` itself only ever stages via `session.add` — this is the
    one place that actually commits it, routed through `write_controller`
    (task-62 §2.2) so it is never in flight at the same time as another
    capture's own commit. Kept as a named function, not a closure, so
    `commit_with_retry`'s redo-from-scratch on a lock error re-invokes it via
    `functools.partial` with THIS call's arguments bound immediately, not a
    closure over a `for` loop variable that has already moved on.
    """
    result = capture_url(
        session, repo, client, archive_url,
        capture_id=capture_id, settings=settings, artifacts_dir=artifacts_dir,
        throttle=throttle, stats=stats, now=now, capture_assets=capture_assets,
        write_controller=write_controller,
    )
    write_controller.submit(session, lambda: None)
    return result


def run_client_capture(
    session: Session,
    client: Client,
    *,
    capture_id: str,
    settings: Settings,
    artifacts_dir: Path,
    write_controller: ArchiveWriteController,
    session_scope: Callable[[], AbstractContextManager[Session]],
    now: datetime | None = None,
    only_url_id: str | None = None,
) -> CaptureOutcome:
    """`only_url_id` (task-62 §2.7, the per-page "Refresh now" button) skips
    seeding and the client's full URL list entirely — the capture is exactly
    one cell, `urls_total=1`.
    """
    now = now or datetime.now(UTC)
    repo = WebArchiveRepository(session)
    # Routed through the controller for the same reason `seed_urls_from_monitor`
    # already batches its own inserts this way (task-59 live incident,
    # 2026-09-04, see its docstring): `get_or_create_settings` flushes a new
    # row on a client's first-ever capture, and a bare call here raced five
    # other clients' own first-flush at the very top of their own
    # `run_client_capture`, live-reproduced 2026-09-05 with every one of them
    # exhausting SQLite's `busy_timeout` at once — the one call in this
    # function with no fetch before it and nothing else protecting it.
    archive_settings = write_controller.submit(
        session, partial(repo.get_or_create_settings, client.id)
    )
    max_urls = archive_settings.max_urls or settings.archive_max_urls_per_client
    capture_assets = archive_settings.capture_assets and settings.archive_capture_assets

    if only_url_id is not None:
        url_ids = [only_url_id]
    else:
        seed_urls_from_monitor(
            session, repo, client, max_urls=max_urls, write_controller=write_controller
        )
        urls = repo.list_urls_for_client(client.id, limit=max_urls)
        ignore_patterns = archive_settings.ignore_patterns_json or []
        if ignore_patterns:
            urls = [u for u in urls if not any(pattern in u.url for pattern in ignore_patterns)]
        # Only ids survive past this point (task-59 live incident, 2026-09-04):
        # `Session.rollback()` expires every ORM instance in the session's
        # identity map by default, including every `ArchiveUrl` already loaded
        # here for URLs further down the list that haven't been processed yet.
        # Holding onto the ORM objects across the loop meant one URL's rollback
        # (a transient `database is locked` under concurrent scheduler load,
        # live-measured) corrupted every LATER cell's object too — the opposite
        # of the "(client, url) cell isolation" PLAN §9.1 requires. Re-fetching
        # a fresh row by id at the top of each iteration makes each cell
        # genuinely independent of whatever happened to the one before it.
        url_ids = [u.id for u in urls]

    outcome = CaptureOutcome(status="ok", urls_total=len(url_ids))
    # Written BEFORE the first URL is touched: without a denominator the
    # hub can only say "in progress…", and a capture of 500 URLs looks
    # identical to a wedged one for the better part of an hour. Best-effort
    # (regression, live 2026-09-05): this is a progress hint, not data the
    # rest of the function depends on — losing it to a lock collision right
    # as six clients bootstrap together must not fail the capture before a
    # single URL has even been tried.
    try:
        write_controller.submit(
            session,
            partial(repo.update_capture_counters, capture_id, urls_total=outcome.urls_total),
        )
    except Exception:  # noqa: BLE001 - best-effort telemetry, see comment above
        session.rollback()
        logger.warning("archive_capture_counters_write_failed capture_id=%s", capture_id)
    last_counter_flush = time.monotonic()
    throttle = HostThrottle(
        min_interval_seconds=settings.archive_host_min_interval_seconds,
        concurrency=settings.archive_host_concurrency,
    )
    stats = FetchStats()
    was_cancelled = False
    is_cancel_requested = _make_cancel_checker(session_scope, capture_id)

    for index, url_id in enumerate(url_ids, start=1):
        # Cooperative cancellation (task-62 §1.6): checked once per URL, not
        # more often — cheap (WAL readers never contend with the write
        # controller), but no point polling faster than the smallest unit
        # of work this loop can actually stop between.
        if is_cancel_requested():
            was_cancelled = True
            break

        archive_url = repo.get_url(url_id)
        if archive_url is None:  # genuinely gone (should not happen; defensive)
            outcome.urls_failed += 1
            continue
        # Read BEFORE the try, not inside the `except` (regression,
        # live 2026-09-05): a failed flush auto-rolls-back the session and
        # SQLAlchemy then refuses any further use — including re-loading an
        # expired attribute — until an explicit `rollback()`, so touching
        # `archive_url.url` for the log message AFTER the exception raised a
        # SECOND, unhandled error there and crashed the whole capture instead
        # of just this one URL.
        archive_url_str = archive_url.url
        try:
            # `capture_url`'s own repo calls (`register_blob`, `create_snapshot`,
            # `touch_url`, ...) flush eagerly and directly — live-reproduced
            # 2026-09-05: `get_or_create_settings`-style contention is not
            # limited to that one call, so a `database is locked` can surface
            # from ANYWHERE inside `capture_url`, not only at the end.
            # `commit_with_retry` wraps the WHOLE call for exactly that
            # reason: on a lock error it rolls back and redoes the entire URL
            # from scratch — safe, because nothing here has committed yet,
            # and `_capture_assets_for_page`'s own per-asset writes are
            # idempotent (an asset already checked this attempt skips
            # re-fetching on the redo). Only the actual commit hops to
            # `write_controller`'s thread (task-62 §2.2) — regression fix,
            # 2026-09-05: six clients' worth of this calling `session.commit()`
            # directly, all finishing pages around the same moment, put the
            # exact pre-task-62 lock storm right back. The fetch — including
            # every asset fetch inside `capture_url` — always runs here, on
            # this (parallel) thread; only the commit is ever serialized.
            # `functools.partial` binds THIS iteration's `archive_url`
            # immediately (unlike a closure over the loop variable) —
            # matters here specifically because the retry above re-invokes
            # the same callable synchronously.
            url_result = commit_with_retry(
                session,
                partial(
                    _capture_url_and_commit, session, repo, client, archive_url,
                    capture_id=capture_id, settings=settings, artifacts_dir=artifacts_dir,
                    throttle=throttle, stats=stats, now=now, capture_assets=capture_assets,
                    write_controller=write_controller,
                ),
            )
        except Exception:  # noqa: BLE001 - one URL's crash must never take down the whole capture
            session.rollback()
            logger.exception(
                "archive_capture_url_crashed client_id=%s url=%s", client.id, archive_url_str
            )
            url_result = UrlCaptureResult(outcome="failed")

        if url_result.outcome == "new":
            outcome.urls_captured += 1
            outcome.urls_new += 1
            outcome.bytes_stored += url_result.bytes_stored
        elif url_result.outcome == "unchanged":
            outcome.urls_captured += 1
            outcome.urls_unchanged += 1
        elif url_result.outcome == "blocked":
            outcome.urls_blocked += 1
        else:
            outcome.urls_failed += 1
        outcome.assets_fetched += url_result.assets_fetched
        outcome.assets_reused += url_result.assets_reused
        outcome.assets_failed += url_result.assets_failed

        # The only per-URL trace of what a capture actually did (task-62
        # §0.5: `run_client_capture` wrote nothing anywhere before this).
        # `debug` for the routine outcomes, loud enough to filter to on the
        # capture-detail page without drowning it by default.
        #
        # Best-effort, not `except Exception` around the URL's OWN capture
        # above (regression, live 2026-09-05): this is telemetry about a URL
        # that already succeeded or was already recorded as failed — losing
        # ONE log line or ONE counter tick to a lock collision must never
        # crash the REST of the capture. Live-caught the same day: with six
        # clients bootstrapping at once, this exact `append_event` call
        # exhausted even the controller's short retry budget and crashed an
        # otherwise-healthy ALPHA capture outright.
        try:
            write_controller.submit(
                session,
                partial(
                    repo.append_event, capture_id,
                    "warning" if url_result.outcome in ("failed", "blocked") else "debug",
                    stage="url", message=f"{archive_url_str} -> {url_result.outcome}",
                ),
            )
        except Exception:  # noqa: BLE001 - best-effort telemetry, see comment above
            session.rollback()
            logger.warning("archive_capture_event_write_failed capture_id=%s", capture_id)

        # Also time-based, not just every Nth URL: a slow client (one URL
        # can take many seconds behind the per-host throttle) otherwise
        # leaves the hub's progress frozen on its last multiple of 20.
        due_by_count = index % _COUNTER_FLUSH_EVERY_URLS == 0 or index == len(url_ids)
        due_by_time = time.monotonic() - last_counter_flush >= _COUNTER_FLUSH_SECONDS
        if due_by_count or due_by_time:
            last_counter_flush = time.monotonic()
            try:
                write_controller.submit(
                    session,
                    partial(
                        repo.update_capture_counters,
                        capture_id,
                        urls_total=outcome.urls_total,
                        urls_captured=outcome.urls_captured, urls_new=outcome.urls_new,
                        urls_unchanged=outcome.urls_unchanged, urls_failed=outcome.urls_failed,
                        urls_blocked=outcome.urls_blocked, assets_fetched=outcome.assets_fetched,
                        assets_reused=outcome.assets_reused, assets_failed=outcome.assets_failed,
                        bytes_stored=outcome.bytes_stored,
                        heartbeat_at=datetime.now(UTC), progress_message=archive_url_str,
                    ),
                )
            except Exception:  # noqa: BLE001 - best-effort telemetry, see comment above
                session.rollback()
                logger.warning("archive_capture_counters_write_failed capture_id=%s", capture_id)

    if was_cancelled:
        outcome.status = "cancelled"
        return outcome

    blocked_ratio = outcome.urls_blocked / outcome.urls_total if outcome.urls_total else 0.0
    if outcome.urls_total > 0 and blocked_ratio >= _BLOCKED_RATIO_THRESHOLD:
        outcome.status = "blocked"
    elif outcome.urls_total > 0 and outcome.urls_failed == outcome.urls_total:
        outcome.status = "failed"
    elif outcome.urls_failed > 0 or outcome.urls_blocked > 0:
        outcome.status = "partial"
    else:
        outcome.status = "ok"

    return outcome


def backfill_css_assets(
    session: Session,
    repo: WebArchiveRepository,
    *,
    client_id: str | None,
    settings: Settings,
    artifacts_dir: Path,
    dry_run: bool,
    now: datetime | None = None,
) -> BackfillOutcome:
    """Retroactively fetches every image/font referenced ONLY from a CSS
    source — an inline `<style>` block or a fetched CSS file's own body —
    that predates task-60's discovery fix and so was never tracked at all
    (task-60 §1.3). Not date-scoped: `WebArchiveRepository.resolve_asset_version`
    falls back to the earliest available version when none exists yet AT a
    requested time, so fetching an asset ONCE, today, retroactively heals
    replay for every existing snapshot that references it (task-60 §0.5).

    `client_id=None` scans the whole portfolio; a given client scopes to
    that client's OWN stored snapshots and the CSS assets THEY reference
    (`ArchiveAsset` itself carries no `client_id` — it is a global,
    content-addressed registry — so scoping goes through
    `ArchiveSnapshot`/`ArchiveSnapshotAsset` instead).
    """
    now = now or datetime.now(UTC)
    outcome = BackfillOutcome(dry_run=dry_run)
    throttle = HostThrottle(
        min_interval_seconds=settings.archive_host_min_interval_seconds,
        concurrency=settings.archive_host_concurrency,
    )
    stats = FetchStats()

    missing: dict[str, assetsmod.AssetRef] = {}

    def _collect(refs: list[assetsmod.AssetRef]) -> None:
        for ref in refs:
            key = normalize_url(ref.url)
            if repo.get_asset_by_key(key) is not None:
                continue  # already tracked — captured or genuinely failed, not this sweep's job
            missing.setdefault(key, ref)

    for snapshot in repo.all_snapshots(client_id):
        outcome.scanned_snapshots += 1
        if not snapshot.body_blob_sha256:
            continue
        try:
            html_bytes = storage.read_blob(artifacts_dir, snapshot.body_blob_sha256)
        except OSError:
            continue
        base_url = snapshot.final_url or ""
        if not base_url:
            continue
        html_text = decode_body(html_bytes, content_type=snapshot.content_type)
        # The full discoverer, not just the inline-`<style>` half of it: any
        # OTHER kind of reference this sweep turns up that isn't tracked yet
        # either is worth backfilling too, at zero extra cost (`_collect`
        # already skips anything `get_asset_by_key` finds).
        _collect(assetsmod.discover_html_assets(html_text, base_url))

    css_assets = repo.css_assets_for_client(client_id) if client_id else repo.all_css_assets()
    for asset in css_assets:
        outcome.scanned_css_files += 1
        css_text = _read_asset_text(artifacts_dir, repo.latest_asset_version(asset.id))
        if css_text:
            _collect(assetsmod.discover_css_assets(css_text, asset.url))

    for ref in missing.values():
        outcome.missing_by_kind[ref.kind] = outcome.missing_by_kind.get(ref.kind, 0) + 1

    if dry_run:
        return outcome

    for key, ref in missing.items():
        asset = repo.get_or_create_asset(ref.url, key, kind=ref.kind)
        try:
            version, result_kind = _check_or_fetch_asset(
                repo, asset, settings=settings, artifacts_dir=artifacts_dir,
                throttle=throttle, stats=stats, now=now,
            )
        except Exception:  # noqa: BLE001 - one bad asset must never abort the whole backfill
            logger.warning("archive_backfill_asset_crashed asset_url=%s", ref.url, exc_info=True)
            version, result_kind = None, "failed"
        commit_with_retry(session, lambda: None)
        if result_kind == "failed" or version is None:
            outcome.failed += 1
        else:
            outcome.fetched += 1

    return outcome
