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
from app.webarchive.urls import normalize_url

logger = logging.getLogger(__name__)

# `Set-Cookie` never reaches storage (PLAN §13: no secrets in stored data or
# events) — a session cookie is exactly the kind of value that must never
# end up replayed back to an operator's browser from an archived response.
_STRIP_RESPONSE_HEADERS = frozenset({"set-cookie"})

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


def _strip_headers(headers: dict[str, str]) -> dict[str, str]:
    return {
        key: value for key, value in headers.items()
        if key.lower() not in _STRIP_RESPONSE_HEADERS
    }


def seed_urls_from_monitor(
    session: Session, repo: WebArchiveRepository, client: Client, *, max_urls: int
) -> int:
    """Every `monitor_pages` row this client already has becomes a tracked
    `ArchiveUrl` (`source="sitemap"`) if it isn't one yet — the monitor's
    own sitemap discovery IS the archive's URL seed list (task-59 §1.9),
    so no separate discovery crawl is needed. Idempotent: `get_or_create_url`
    no-ops for a URL already tracked.
    """
    stmt = select(MonitorPage.url).where(MonitorPage.client_id == client.id).limit(max_urls)
    added = 0
    for (url,) in session.execute(stmt).all():
        before = repo.find_url(client.id, normalize_url(url))
        repo.get_or_create_url(client.id, url, normalize_url(url), source="sitemap")
        if before is None:
            added += 1
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


def _capture_assets_for_page(
    repo: WebArchiveRepository,
    html_text: str,
    *,
    base_url: str,
    settings: Settings,
    artifacts_dir: Path,
    throttle: HostThrottle,
    stats: FetchStats,
    now: datetime,
) -> _AssetCaptureOutcome:
    outcome = _AssetCaptureOutcome()
    remaining_new_budget = settings.archive_new_assets_per_page

    for ref in assetsmod.discover_html_assets(html_text, base_url):
        key = normalize_url(ref.url)
        asset = repo.get_or_create_asset(ref.url, key, kind=ref.kind)
        last_checked_at = asset.last_checked_at
        never_checked = last_checked_at is None
        needs_check = never_checked or (
            last_checked_at is not None
            and now - last_checked_at >= timedelta(days=settings.archive_asset_revalidate_days)
        )

        if not needs_check:
            version = repo.latest_asset_version(asset.id)
            outcome.links.append((asset.id, version.id if version else None, ref.kind))
            outcome.reused += 1
            continue

        if never_checked and remaining_new_budget <= 0:
            outcome.truncated = True
            outcome.links.append((asset.id, None, ref.kind))
            outcome.failed += 1
            continue

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
        outcome.links.append((asset.id, version.id if version else None, ref.kind))
        if result_kind == "fetched":
            outcome.fetched += 1
            if never_checked:
                remaining_new_budget -= 1
        elif result_kind == "revalidated":
            outcome.reused += 1
        else:
            outcome.failed += 1

    return outcome


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

    html_text = decode_body(result.body, content_type=result.headers.get("Content-Type"))
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
            repo, html_text, base_url=result.final_url, settings=settings,
            artifacts_dir=artifacts_dir, throttle=throttle, stats=stats, now=now,
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
        key = normalize_url(link)
        if repo.find_url(client.id, key) is None:
            repo.get_or_create_url(client.id, link, key, source="link")


def run_client_capture(
    session: Session,
    client: Client,
    *,
    capture_id: str,
    settings: Settings,
    artifacts_dir: Path,
    now: datetime | None = None,
) -> CaptureOutcome:
    now = now or datetime.now(UTC)
    repo = WebArchiveRepository(session)
    archive_settings = repo.get_or_create_settings(client.id)
    max_urls = archive_settings.max_urls or settings.archive_max_urls_per_client
    capture_assets = archive_settings.capture_assets and settings.archive_capture_assets

    seed_urls_from_monitor(session, repo, client, max_urls=max_urls)
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
    throttle = HostThrottle(
        min_interval_seconds=settings.archive_host_min_interval_seconds,
        concurrency=settings.archive_host_concurrency,
    )
    stats = FetchStats()

    for index, url_id in enumerate(url_ids, start=1):
        archive_url = repo.get_url(url_id)
        if archive_url is None:  # genuinely gone (should not happen; defensive)
            outcome.urls_failed += 1
            continue
        try:
            url_result = commit_with_retry(
                session,
                # `functools.partial` binds THIS iteration's `archive_url`
                # immediately (unlike a lambda closing over the loop
                # variable) — matters here specifically because a retry
                # re-invokes the same callable synchronously.
                partial(
                    capture_url, session, repo, client, archive_url,
                    capture_id=capture_id, settings=settings, artifacts_dir=artifacts_dir,
                    throttle=throttle, stats=stats, now=now, capture_assets=capture_assets,
                ),
            )
        except Exception:  # noqa: BLE001 - one URL's crash must never take down the whole capture
            logger.exception(
                "archive_capture_url_crashed client_id=%s url=%s", client.id, archive_url.url
            )
            session.rollback()
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

        if index % 20 == 0 or index == len(url_ids):
            repo.update_capture_counters(
                capture_id,
                urls_captured=outcome.urls_captured, urls_new=outcome.urls_new,
                urls_unchanged=outcome.urls_unchanged, urls_failed=outcome.urls_failed,
                urls_blocked=outcome.urls_blocked, assets_fetched=outcome.assets_fetched,
                assets_reused=outcome.assets_reused, assets_failed=outcome.assets_failed,
                bytes_stored=outcome.bytes_stored,
            )
            session.commit()

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
