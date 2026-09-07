"""Web Archive REST API (task-59 §6), prefix `/api/v1/archive`.

Self-registering router — `app/main.py` needs no edit. Read side never
triggers a capture; the worker's `ArchiveScheduler` is the sole claimer and
executor (same enqueue/claim split `app/api/monitor.py`'s "Check now" uses
for `MonitorRun`). Replay itself (`GET /archive/r/...`) lives in
`app/api/archive_replay.py`, outside `/api/v1`, so its URLs stay short and
readable in an iframe's own address bar.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import PlainTextResponse

from app.api import register_router
from app.core.config import get_settings
from app.db.models import ArchiveCapture, ArchiveSnapshot, ArchiveUrl
from app.db.repositories.clients import ClientRepository
from app.db.repositories.web_archive import WebArchiveRepository
from app.db.session import worker_session_scope
from app.monitor.page import decode_body
from app.schemas.api import ErrorDTO
from app.schemas.web_archive import (
    ArchiveAdhocCaptureAcceptedDTO,
    ArchiveCaptureAcceptedDTO,
    ArchiveCaptureBatchAcceptedDTO,
    ArchiveCaptureCancelDTO,
    ArchiveCaptureDTO,
    ArchiveCaptureEventDTO,
    ArchiveChangeDiffDTO,
    ArchiveChangeDTO,
    ArchiveChangesPageDTO,
    ArchiveClientCoverageDTO,
    ArchiveLookupDTO,
    ArchiveOverviewDTO,
    ArchiveQueueDTO,
    ArchiveSnapshotDetailDTO,
    ArchiveSnapshotSummaryDTO,
    ArchiveUrlDTO,
    ArchiveUrlSearchResponseDTO,
    ArchiveUrlSearchResultDTO,
    ArchiveUrlTimelineDTO,
)
from app.urlinspector.resolve import resolve_client_for_url
from app.webarchive import changes as changesmod
from app.webarchive import rewrite as rewritemod
from app.webarchive import storage
from app.webarchive.db_retry import commit_with_retry
from app.webarchive.replay import format_ts
from app.webarchive.urls import normalize_url

router = APIRouter(prefix="/api/v1/archive", tags=["web-archive"])

_URL_NOT_FOUND = ErrorDTO(
    error_code="url_not_found", message="URL not tracked by the archive"
).model_dump()
_SNAPSHOT_NOT_FOUND = ErrorDTO(
    error_code="snapshot_not_found", message="Snapshot not found"
).model_dump()
_CLIENT_NOT_FOUND = ErrorDTO(
    error_code="client_not_found", message="Client not found"
).model_dump()
_CAPTURE_NOT_FOUND = ErrorDTO(
    error_code="capture_not_found", message="Capture not found"
).model_dump()
_ALREADY_RUNNING = ErrorDTO(
    error_code="already_running", message="A capture is already running for this client"
).model_dump()
_NO_PREVIOUS = ErrorDTO(
    error_code="no_previous_snapshot", message="This is the first snapshot — nothing to compare"
).model_dump()


def _name_or(mapping: dict, key: str) -> str:  # type: ignore[type-arg]
    entry = mapping.get(key)
    return entry.name if entry is not None else "?"


def _url_dto(archive_url: ArchiveUrl, client_name: str) -> ArchiveUrlDTO:
    return ArchiveUrlDTO(
        id=archive_url.id,
        client_id=archive_url.client_id,
        client_name=client_name,
        url=archive_url.url,
        source=archive_url.source,  # type: ignore[arg-type]
        first_captured_at=archive_url.first_captured_at,
        last_captured_at=archive_url.last_captured_at,
        snapshot_count=archive_url.snapshot_count,
        gone_since=archive_url.gone_since,
    )


def _capture_dto(
    capture: ArchiveCapture,
    client_name: str,
    *,
    target_url: str | None = None,
    queue_position: int | None = None,
) -> ArchiveCaptureDTO:
    return ArchiveCaptureDTO(
        id=capture.id,
        client_id=capture.client_id,
        client_name=client_name,
        slot_key=capture.slot_key,
        scheduled_for=capture.scheduled_for,
        trigger=capture.trigger,  # type: ignore[arg-type]
        status=capture.status,  # type: ignore[arg-type]
        started_at=capture.started_at,
        finished_at=capture.finished_at,
        urls_total=capture.urls_total,
        urls_captured=capture.urls_captured,
        urls_unchanged=capture.urls_unchanged,
        urls_new=capture.urls_new,
        urls_failed=capture.urls_failed,
        urls_blocked=capture.urls_blocked,
        assets_fetched=capture.assets_fetched,
        assets_reused=capture.assets_reused,
        assets_failed=capture.assets_failed,
        bytes_stored=capture.bytes_stored,
        error_code=capture.error_code,
        error_message=capture.error_message,
        scope=capture.scope,  # type: ignore[arg-type]
        target_url=target_url,
        progress_message=capture.progress_message,
        cancel_requested=capture.cancel_requested,
        queue_position=queue_position,
        force=capture.force,
    )


def _resolve_target_url(repo: WebArchiveRepository, capture: ArchiveCapture) -> str | None:
    if capture.scope != "url" or capture.archive_url_id is None:
        return None
    archive_url = repo.get_url(capture.archive_url_id)
    return archive_url.url if archive_url is not None else None


def _snapshot_summary_dto(
    snapshot: ArchiveSnapshot, *, has_change: bool
) -> ArchiveSnapshotSummaryDTO:
    return ArchiveSnapshotSummaryDTO(
        id=snapshot.id,
        ts=format_ts(snapshot.observed_at),
        observed_at=snapshot.observed_at,
        last_confirmed_at=snapshot.last_confirmed_at,
        confirmed_count=snapshot.confirmed_count,
        http_status=snapshot.http_status,
        store_reason=snapshot.store_reason,  # type: ignore[arg-type]
        title=snapshot.title,
        asset_count=snapshot.asset_count,
        assets_truncated=snapshot.assets_truncated,
        has_change=has_change,
    )


def _change_dto(change, client_name: str, url: str) -> ArchiveChangeDTO:  # type: ignore[no-untyped-def]
    return ArchiveChangeDTO(
        id=change.id,
        client_id=change.client_id,
        client_name=client_name,
        archive_url_id=change.archive_url_id,
        url=url,
        snapshot_id=change.snapshot_id,
        prev_snapshot_id=change.prev_snapshot_id,
        detected_at=change.detected_at,
        change_kinds=change.change_kinds_json,
        summary=change.summary_json,
        text_delta_words=change.text_delta_words,
    )


# --- overview ----------------------------------------------------------


def _overview() -> ArchiveOverviewDTO:
    settings = get_settings()
    with worker_session_scope() as session:
        repo = WebArchiveRepository(session)
        clients_repo = ClientRepository(session)
        clients = clients_repo.list_active()
        all_settings = repo.all_settings_by_client()
        recent = repo.list_captures(limit=10)
        blob_stats = repo.blob_stats()
        asset_coverage = repo.asset_coverage_totals()

        coverage: list[ArchiveClientCoverageDTO] = []
        for client in clients:
            archive_settings = all_settings.get(client.id)
            client_captures = repo.list_captures(client.id, limit=1)
            last_capture = client_captures[0] if client_captures else None
            coverage.append(
                ArchiveClientCoverageDTO(
                    client_id=client.id,
                    client_name=client.name,
                    enabled=archive_settings.enabled if archive_settings else True,
                    urls_tracked=repo.count_urls_for_client(client.id),
                    urls_known_to_monitor=repo.monitor_known_url_count(client.id),
                    snapshots_total=repo.count_snapshots_for_client(client.id),
                    last_capture_at=last_capture.finished_at if last_capture else None,
                    last_capture_status=last_capture.status if last_capture else None,  # type: ignore[arg-type]
                    next_capture_at=None,
                )
            )

        top_urls = repo.top_changing_urls(limit=5)
        client_names = repo.clients_by_id([u.client_id for u in top_urls])
        top_dtos = [_url_dto(u, _name_or(client_names, u.client_id)) for u in top_urls]
        recent_client_names = repo.clients_by_id([c.client_id for c in recent])
        recent_dtos = [
            _capture_dto(
                c, _name_or(recent_client_names, c.client_id),
                target_url=_resolve_target_url(repo, c),
            )
            for c in recent
        ]

        return ArchiveOverviewDTO(
            archive_enabled=settings.archive_enabled,
            cloud_enabled=settings.cloud_enabled,
            timezone=settings.archive_timezone,
            capture_times=settings.archive_capture_times,
            clients=coverage,
            urls_total=sum(c.urls_tracked for c in coverage),
            snapshots_total=repo.count_snapshots_total(),
            blobs_total=blob_stats["blobs_total"],
            bytes_local=blob_stats["bytes_local"],
            bytes_uploaded=blob_stats["bytes_uploaded"],
            bytes_not_uploaded=blob_stats["bytes_not_uploaded"],
            assets_referenced=asset_coverage["referenced"],
            assets_captured=asset_coverage["captured"],
            top_changing_urls=top_dtos,
            recent_captures=recent_dtos,
        )


@router.get("/overview")
async def get_archive_overview() -> ArchiveOverviewDTO:
    return await run_in_threadpool(_overview)


# --- lookup / search -----------------------------------------------------


def _lookup(url: str) -> ArchiveLookupDTO:
    key = normalize_url(url)
    with worker_session_scope() as session:
        repo = WebArchiveRepository(session)
        archive_url = repo.find_url_any_client(key)
        if archive_url is None:
            # task-70: even an UNTRACKED URL may belong to one of our
            # clients (its host just hasn't been captured yet, or ever
            # linked-to) — resolve that now so the caller can offer/deny
            # "Archive this page now" without a round trip that fails.
            clients_repo = ClientRepository(session)
            resolved = resolve_client_for_url(url, clients_repo.list_active())
            if resolved.client is None:
                return ArchiveLookupDTO(found=False)
            return ArchiveLookupDTO(
                found=False, client_id=resolved.client.id, client_name=resolved.client.name,
                capturable=True,
            )
        client_name = repo.client_name(archive_url.client_id) or "?"
        return ArchiveLookupDTO(
            found=True, url=_url_dto(archive_url, client_name),
            client_id=archive_url.client_id, client_name=client_name, capturable=True,
        )


@router.get("/lookup")
async def lookup_archive_url(url: str = Query(...)) -> ArchiveLookupDTO:
    return await run_in_threadpool(_lookup, url)


def _search_urls(
    client_id: str | None, q: str | None, page: int, page_size: int
) -> ArchiveUrlSearchResponseDTO:
    with worker_session_scope() as session:
        repo = WebArchiveRepository(session)
        rows, _total = repo.search_urls(
            client_id=client_id, query=q, page=page, page_size=page_size
        )
        client_names = repo.clients_by_id([r.client_id for r in rows])
        items = [
            ArchiveUrlSearchResultDTO(
                id=row.id,
                client_id=row.client_id,
                client_name=_name_or(client_names, row.client_id),
                url=row.url,
                snapshot_count=row.snapshot_count,
                last_captured_at=row.last_captured_at,
            )
            for row in rows
        ]
        return ArchiveUrlSearchResponseDTO(items=items)


@router.get("/urls")
async def search_archive_urls(
    client_id: str | None = None, q: str | None = None, page: int = 1, page_size: int = 20
) -> ArchiveUrlSearchResponseDTO:
    return await run_in_threadpool(_search_urls, client_id, q, page, page_size)


# --- one URL's timeline --------------------------------------------------


def _timeline(url_id: str) -> ArchiveUrlTimelineDTO | None:
    with worker_session_scope() as session:
        repo = WebArchiveRepository(session)
        archive_url = repo.get_url(url_id)
        if archive_url is None:
            return None
        client_name = repo.client_name(archive_url.client_id) or "?"
        snapshots = repo.list_snapshots_for_url(url_id)
        summaries = [
            _snapshot_summary_dto(s, has_change=repo.has_change_for_snapshot(s.id))
            for s in snapshots
        ]
        return ArchiveUrlTimelineDTO(url=_url_dto(archive_url, client_name), snapshots=summaries)


@router.get("/urls/{url_id}/snapshots", responses={404: {"model": ErrorDTO}})
async def get_url_timeline(url_id: str) -> ArchiveUrlTimelineDTO:
    result = await run_in_threadpool(_timeline, url_id)
    if result is None:
        raise HTTPException(status_code=404, detail=_URL_NOT_FOUND)
    return result


# --- one snapshot's detail -------------------------------------------------


def _snapshot_detail(snapshot_id: str) -> ArchiveSnapshotDetailDTO | None:
    settings = get_settings()
    with worker_session_scope() as session:
        repo = WebArchiveRepository(session)
        snapshot = repo.get_snapshot(snapshot_id)
        if snapshot is None:
            return None
        archive_url = repo.get_url(snapshot.archive_url_id)
        assert archive_url is not None
        client_name = repo.client_name(snapshot.client_id) or "?"
        siblings = repo.list_snapshots_for_url(snapshot.archive_url_id)  # newest first
        ordered = list(reversed(siblings))  # oldest first
        index = next((i for i, s in enumerate(ordered) if s.id == snapshot.id), None)
        prev_id = ordered[index - 1].id if index is not None and index > 0 else None
        next_id = (
            ordered[index + 1].id if index is not None and index + 1 < len(ordered) else None
        )
        asset_links = repo.snapshot_assets(snapshot.id)
        assets_missing = sum(1 for _sa, _asset, version in asset_links if version is None)
        live_hosts: list[str] = []
        if snapshot.body_blob_sha256:
            try:
                raw_html = storage.read_blob(settings.artifacts_dir, snapshot.body_blob_sha256)
            except OSError:
                raw_html = None
            if raw_html is not None:
                html_text = decode_body(raw_html, content_type=snapshot.content_type)
                live_hosts = rewritemod.live_iframe_hosts(
                    html_text,
                    base_url=snapshot.final_url or archive_url.url,
                    live_iframes=settings.archive_live_iframes,
                    analytics_hosts=frozenset(settings.archive_iframe_analytics_hosts),
                )
        ts = format_ts(snapshot.observed_at)
        host = urlsplit(snapshot.final_url or archive_url.url).hostname or ""
        replay_misses_for_host = repo.count_replay_misses_for_host(host) if host else 0
        return ArchiveSnapshotDetailDTO(
            id=snapshot.id,
            archive_url_id=snapshot.archive_url_id,
            client_id=snapshot.client_id,
            client_name=client_name,
            url=archive_url.url,
            ts=ts,
            view_url=f"/archive/r/{ts}/{archive_url.url}",
            observed_at=snapshot.observed_at,
            last_confirmed_at=snapshot.last_confirmed_at,
            confirmed_count=snapshot.confirmed_count,
            http_status=snapshot.http_status,
            final_url=snapshot.final_url,
            content_type=snapshot.content_type,
            title=snapshot.title,
            description=snapshot.description,
            canonical=snapshot.canonical,
            meta_robots=snapshot.meta_robots,
            word_count=snapshot.word_count,
            headers=snapshot.headers_json or {},
            asset_count=snapshot.asset_count,
            assets_truncated=snapshot.assets_truncated,
            assets_missing=assets_missing,
            live_iframe_hosts=live_hosts,
            store_reason=snapshot.store_reason,  # type: ignore[arg-type]
            prev_snapshot_id=prev_id,
            next_snapshot_id=next_id,
            replay_misses_for_host=replay_misses_for_host,
        )


@router.get("/snapshots/{snapshot_id}", responses={404: {"model": ErrorDTO}})
async def get_snapshot_detail(snapshot_id: str) -> ArchiveSnapshotDetailDTO:
    result = await run_in_threadpool(_snapshot_detail, snapshot_id)
    if result is None:
        raise HTTPException(status_code=404, detail=_SNAPSHOT_NOT_FOUND)
    return result


def _snapshot_source(snapshot_id: str) -> str | None:
    settings = get_settings()
    with worker_session_scope() as session:
        repo = WebArchiveRepository(session)
        snapshot = repo.get_snapshot(snapshot_id)
        if snapshot is None or snapshot.body_blob_sha256 is None:
            return None
        raw = storage.read_blob(settings.artifacts_dir, snapshot.body_blob_sha256)
        return raw.decode("utf-8", errors="replace")


@router.get("/snapshots/{snapshot_id}/source", responses={404: {"model": ErrorDTO}})
async def get_snapshot_source(snapshot_id: str) -> PlainTextResponse:
    result = await run_in_threadpool(_snapshot_source, snapshot_id)
    if result is None:
        raise HTTPException(status_code=404, detail=_SNAPSHOT_NOT_FOUND)
    return PlainTextResponse(result)


def _snapshot_diff(snapshot_id: str) -> ArchiveChangeDiffDTO | str:
    settings = get_settings()
    with worker_session_scope() as session:
        repo = WebArchiveRepository(session)
        snapshot = repo.get_snapshot(snapshot_id)
        if snapshot is None:
            return "snapshot_not_found"
        if snapshot.prev_snapshot_id is None:
            return "no_previous_snapshot"
        prev = repo.get_snapshot(snapshot.prev_snapshot_id)
        if prev is None or prev.body_blob_sha256 is None or snapshot.body_blob_sha256 is None:
            return "no_previous_snapshot"
        old_text = storage.read_blob(settings.artifacts_dir, prev.body_blob_sha256).decode(
            "utf-8", errors="replace"
        )
        new_text = storage.read_blob(settings.artifacts_dir, snapshot.body_blob_sha256).decode(
            "utf-8", errors="replace"
        )
        diff_lines = changesmod.unified_text_diff(old_text, new_text)
        client_name = repo.client_name(snapshot.client_id) or "?"
        archive_url = repo.get_url(snapshot.archive_url_id)
        change_row = repo.get_change_by_snapshot(snapshot.id)
        change_dto = (
            _change_dto(change_row, client_name, archive_url.url if archive_url else "")
            if change_row is not None
            else ArchiveChangeDTO(
                id="", client_id=snapshot.client_id, client_name=client_name,
                archive_url_id=snapshot.archive_url_id, url=archive_url.url if archive_url else "",
                snapshot_id=snapshot.id, prev_snapshot_id=prev.id, detected_at=snapshot.observed_at,
                change_kinds=[], summary={}, text_delta_words=0,
            )
        )
        return ArchiveChangeDiffDTO(change=change_dto, diff_lines=diff_lines)


@router.get("/snapshots/{snapshot_id}/diff", responses={404: {"model": ErrorDTO}})
async def get_snapshot_diff(snapshot_id: str) -> ArchiveChangeDiffDTO:
    result = await run_in_threadpool(_snapshot_diff, snapshot_id)
    if result == "snapshot_not_found":
        raise HTTPException(status_code=404, detail=_SNAPSHOT_NOT_FOUND)
    if result == "no_previous_snapshot":
        raise HTTPException(status_code=404, detail=_NO_PREVIOUS)
    assert isinstance(result, ArchiveChangeDiffDTO)
    return result


# --- changes feed --------------------------------------------------------


def _changes(
    client_id: str | None, kind: str | None, days: int, page: int, page_size: int
) -> ArchiveChangesPageDTO:
    with worker_session_scope() as session:
        repo = WebArchiveRepository(session)
        start = datetime.now(UTC) - timedelta(days=days) if days > 0 else None
        rows, total = repo.list_changes(
            client_id=client_id, kind=kind, start=start, end=None, page=page, page_size=page_size
        )
        client_names = repo.clients_by_id([r.client_id for r in rows])
        url_by_id = {r.archive_url_id: repo.get_url(r.archive_url_id) for r in rows}
        def _url_or_blank(archive_url_id: str) -> str:
            found = url_by_id.get(archive_url_id)
            return found.url if found is not None else ""

        items = [
            _change_dto(
                row, _name_or(client_names, row.client_id), _url_or_blank(row.archive_url_id)
            )
            for row in rows
        ]
        return ArchiveChangesPageDTO(items=items, total=total, page=page, page_size=page_size)


@router.get("/changes")
async def list_archive_changes(
    client_id: str | None = None,
    kind: str | None = None,
    days: int = 30,
    page: int = 1,
    page_size: int = 50,
) -> ArchiveChangesPageDTO:
    return await run_in_threadpool(_changes, client_id, kind, days, page, page_size)


# --- captures --------------------------------------------------------------


def _captures(client_id: str | None, limit: int) -> list[ArchiveCaptureDTO]:
    with worker_session_scope() as session:
        repo = WebArchiveRepository(session)
        rows = repo.list_captures(client_id, limit=limit)
        client_names = repo.clients_by_id([r.client_id for r in rows])
        return [
            _capture_dto(
                row, _name_or(client_names, row.client_id),
                target_url=_resolve_target_url(repo, row),
            )
            for row in rows
        ]


@router.get("/captures")
async def list_archive_captures(
    client_id: str | None = None, limit: int = 50
) -> list[ArchiveCaptureDTO]:
    return await run_in_threadpool(_captures, client_id, limit)


def _start_capture(client_id: str) -> str:
    with worker_session_scope() as session:
        clients_repo = ClientRepository(session)
        client = clients_repo.get(client_id)
        if client is None:
            return "client_not_found"
        repo = WebArchiveRepository(session)
        # Both `running` AND `queued` (task-62 §1.3) — guarding only
        # `running` let two rapid clicks queue two rows for the same client
        # before the scheduler's next tick claimed the first one.
        if repo.has_active_capture(client_id):
            return "already_running"
        slot_key = f"manual:{uuid.uuid4()}"

        def _create() -> ArchiveCapture:
            return repo.create_capture(
                client_id, slot_key=slot_key, scheduled_for=datetime.now(UTC), trigger="manual"
            )

        # This API process has no `ArchiveWriteController` of its own to
        # route through (that only lives inside the worker's scheduler loop,
        # task-62 §2.2) — a click here can land while the worker is mid-tick
        # across up to `archive_client_workers` clients, live-observed
        # 2026-09-05 raising a bare `database is locked` straight to a 500.
        # `commit_with_retry` is the same cross-process fallback the CLI
        # already relies on for this exact reason (`writequeue.py`'s own
        # docstring): rare tail, but no reason to leave it unretried.
        capture = commit_with_retry(session, _create)
        return capture.id


@router.post(
    "/captures",
    status_code=202,
    responses={404: {"model": ErrorDTO}, 409: {"model": ErrorDTO}},
)
async def start_archive_capture(client_id: str) -> ArchiveCaptureAcceptedDTO:
    """"Capture now" — enqueues a `queued` `ArchiveCapture` for the worker's
    `ArchiveScheduler` to claim on its next tick (≤60s later); the worker
    process never crawls a client's site from the API itself.
    """
    result = await run_in_threadpool(_start_capture, client_id)
    if result == "client_not_found":
        raise HTTPException(status_code=404, detail=_CLIENT_NOT_FOUND)
    if result == "already_running":
        raise HTTPException(status_code=409, detail=_ALREADY_RUNNING)
    return ArchiveCaptureAcceptedDTO(
        capture_id=result, status_url=f"/api/v1/archive/captures/{result}"
    )


def _start_forced_capture(client_id: str) -> str:
    """"Recapture" (task-80) — a copy of `_start_capture` with `force=True`
    on the row. Separate function, separate route: the worker reads `force`
    back off the row (`scheduler.py`'s own `_submit`), not off the request,
    so nothing here differs except that one flag — but a distinct route
    keeps this distinguishable in the audit log (`archive.recapture_start`
    vs `archive.capture_start`), which a shared endpoint with a body flag
    would not.
    """
    with worker_session_scope() as session:
        clients_repo = ClientRepository(session)
        client = clients_repo.get(client_id)
        if client is None:
            return "client_not_found"
        repo = WebArchiveRepository(session)
        # Same guard as an ordinary capture (§0.7 of the task plan): forcing
        # a fresh copy does not bypass the "one active capture per client"
        # invariant `claim_capture` itself enforces.
        if repo.has_active_capture(client_id):
            return "already_running"
        slot_key = f"manual:{uuid.uuid4()}"

        def _create() -> ArchiveCapture:
            return repo.create_capture(
                client_id, slot_key=slot_key, scheduled_for=datetime.now(UTC), trigger="manual",
                force=True,
            )

        # Same cross-process retry reasoning as `_start_capture` above.
        capture = commit_with_retry(session, _create)
        return capture.id


@router.post(
    "/captures/force",
    status_code=202,
    responses={404: {"model": ErrorDTO}, 409: {"model": ErrorDTO}},
)
async def start_forced_archive_capture(client_id: str) -> ArchiveCaptureAcceptedDTO:
    """"Recapture" — stores a new snapshot for every tracked URL of this
    client, even the ones that didn't change, instead of `confirm_snapshot`
    bumping the old row's `confirmed_count` (task-80). Same queue, same
    live-progress page as "Capture now" — only the `force` flag differs.
    """
    result = await run_in_threadpool(_start_forced_capture, client_id)
    if result == "client_not_found":
        raise HTTPException(status_code=404, detail=_CLIENT_NOT_FOUND)
    if result == "already_running":
        raise HTTPException(status_code=409, detail=_ALREADY_RUNNING)
    return ArchiveCaptureAcceptedDTO(
        capture_id=result, status_url=f"/api/v1/archive/captures/{result}"
    )


def _start_url_capture(url_id: str) -> str:
    """"Refresh now" on one URL (task-62 §1.1/§2.7) — `urls_total=1`,
    seconds, not the whole client's list.

    Guards only a duplicate capture of THIS SAME URL (task-70), not any
    OTHER capture on the same client (a scheduled portfolio pass can run
    for hours) — `has_active_url_capture`'s own docstring covers why this
    is still safe: `claim_capture` is the actual admission gate into
    `running`, and it still refuses one per client regardless of how many
    `queued` rows exist.
    """
    with worker_session_scope() as session:
        repo = WebArchiveRepository(session)
        archive_url = repo.get_url(url_id)
        if archive_url is None:
            return "url_not_found"
        if repo.has_active_url_capture(archive_url.client_id, url_id):
            return "already_running"
        slot_key = f"manual:{uuid.uuid4()}"

        def _create() -> ArchiveCapture:
            return repo.create_capture(
                archive_url.client_id, slot_key=slot_key, scheduled_for=datetime.now(UTC),
                trigger="manual", scope="url", archive_url_id=url_id,
            )

        # Cross-process retry, same reasoning as `_start_capture` above.
        capture = commit_with_retry(session, _create)
        return capture.id


@router.post(
    "/captures/url",
    status_code=202,
    responses={404: {"model": ErrorDTO}, 409: {"model": ErrorDTO}},
)
async def start_archive_url_capture(url_id: str) -> ArchiveCaptureAcceptedDTO:
    result = await run_in_threadpool(_start_url_capture, url_id)
    if result == "url_not_found":
        raise HTTPException(status_code=404, detail=_URL_NOT_FOUND)
    if result == "already_running":
        raise HTTPException(status_code=409, detail=_ALREADY_RUNNING)
    return ArchiveCaptureAcceptedDTO(
        capture_id=result, status_url=f"/api/v1/archive/captures/{result}"
    )


def _start_adhoc_url_capture(url: str) -> tuple[str, str, str, str] | str:
    """"Archive this page now" on a URL the archive has NEVER seen (task-63
    follow-up, live-reported 2026-09-05): `find_url_any_client` in `/lookup`
    only ever finds an ALREADY-tracked URL, so a genuinely new page had no
    path forward besides waiting for the next portfolio-wide discovery
    pass. Resolves the client from the URL's own host (the same helper URL
    Inspector uses to answer the identical question), creates the
    `ArchiveUrl` row on the spot, and enqueues exactly the `scope="url"`
    capture `_start_url_capture` above enqueues for an already-tracked one.
    """
    with worker_session_scope() as session:
        clients_repo = ClientRepository(session)
        resolved = resolve_client_for_url(url, clients_repo.list_active())
        if resolved.client is None:
            return "client_not_found"
        client = resolved.client
        repo = WebArchiveRepository(session)
        # Scoped to THIS URL, not the whole client (task-70) — a scheduled
        # portfolio-wide capture can run for hours, and it must not block
        # archiving one page of the same client meanwhile. A genuinely new
        # URL (no row yet) can have no `scope="url"` capture against it, so
        # the check only matters once `existing` is found.
        existing = repo.find_url(client.id, normalize_url(url))
        if existing is not None and repo.has_active_url_capture(client.id, existing.id):
            return "already_running"
        def _create() -> tuple[ArchiveUrl, ArchiveCapture]:
            url_row = repo.get_or_create_url(client.id, url, normalize_url(url), source="manual")
            # `get_or_create_url` no longer flushes itself (task-65) — a
            # brand-new row's id is only safe to use as another row's FK
            # within the SAME transaction after an explicit flush here
            # (live-confirmed 2026-09-05: `create_capture` below raised
            # `FOREIGN KEY constraint failed` without this).
            repo.flush()
            capture_row = repo.create_capture(
                client.id, slot_key=f"manual:{uuid.uuid4()}", scheduled_for=datetime.now(UTC),
                trigger="manual", scope="url", archive_url_id=url_row.id,
            )
            return url_row, capture_row

        # Cross-process retry, same reasoning as `_start_capture` above.
        archive_url, capture = commit_with_retry(session, _create)
        return capture.id, archive_url.id, client.id, client.name


@router.post(
    "/captures/url-adhoc",
    status_code=202,
    responses={404: {"model": ErrorDTO}, 409: {"model": ErrorDTO}},
)
async def start_archive_adhoc_url_capture(url: str) -> ArchiveAdhocCaptureAcceptedDTO:
    result = await run_in_threadpool(_start_adhoc_url_capture, url)
    if result == "client_not_found":
        raise HTTPException(status_code=404, detail=_CLIENT_NOT_FOUND)
    if result == "already_running":
        raise HTTPException(status_code=409, detail=_ALREADY_RUNNING)
    assert not isinstance(result, str)
    capture_id, url_id, client_id, client_name = result
    return ArchiveAdhocCaptureAcceptedDTO(
        capture_id=capture_id, status_url=f"/api/v1/archive/captures/{capture_id}",
        url_id=url_id, client_id=client_id, client_name=client_name,
    )


def _start_all_captures() -> tuple[list[str], list[str]]:
    """"Capture all" (task-62 §1.2) — one `queued` row per collectable
    client, skipping any that already has one active. The FIFO queue
    (§1.3) is what makes this safe to fire for the whole portfolio at once
    instead of a 7-hour foreground operation (§0.6).
    """
    with worker_session_scope() as session:
        clients_repo = ClientRepository(session)
        repo = WebArchiveRepository(session)

        def _create_all() -> tuple[list[str], list[str]]:
            created: list[str] = []
            skipped: list[str] = []
            for client in clients_repo.list_collectable():
                if repo.has_active_capture(client.id):
                    skipped.append(client.id)
                    continue
                capture = repo.create_capture(
                    client.id, slot_key=f"manual:{uuid.uuid4()}",
                    scheduled_for=datetime.now(UTC), trigger="manual",
                )
                created.append(capture.id)
            return created, skipped

        # Cross-process retry, same reasoning as `_start_capture` above — a
        # whole-batch redo on a lock error is safe: nothing here committed,
        # so rolling back and re-running the loop (fresh `uuid4()` slot keys)
        # just recreates the same logical set of rows, never a duplicate.
        return commit_with_retry(session, _create_all)


@router.post("/captures/all", status_code=202)
async def start_all_archive_captures() -> ArchiveCaptureBatchAcceptedDTO:
    created, skipped = await run_in_threadpool(_start_all_captures)
    return ArchiveCaptureBatchAcceptedDTO(capture_ids=created, skipped_client_ids=skipped)


def _capture_detail(capture_id: str) -> ArchiveCaptureDTO | None:
    with worker_session_scope() as session:
        repo = WebArchiveRepository(session)
        capture = repo.get_capture(capture_id)
        if capture is None:
            return None
        client_name = repo.client_name(capture.client_id) or "?"
        queue_position = None
        if capture.status == "queued":
            queued_ids = [c.id for c in repo.queued_captures()]
            if capture.id in queued_ids:
                queue_position = queued_ids.index(capture.id) + 1
        return _capture_dto(
            capture, client_name,
            target_url=_resolve_target_url(repo, capture), queue_position=queue_position,
        )


@router.get("/captures/{capture_id}", responses={404: {"model": ErrorDTO}})
async def get_archive_capture(capture_id: str) -> ArchiveCaptureDTO:
    result = await run_in_threadpool(_capture_detail, capture_id)
    if result is None:
        raise HTTPException(status_code=404, detail=_CAPTURE_NOT_FOUND)
    return result


def _capture_events(capture_id: str, since: int) -> list[ArchiveCaptureEventDTO] | None:
    with worker_session_scope() as session:
        repo = WebArchiveRepository(session)
        if repo.get_capture(capture_id) is None:
            return None
        rows = repo.events_since(capture_id, since)
        return [
            ArchiveCaptureEventDTO(
                seq=row.seq,
                level=row.level,  # type: ignore[arg-type]
                stage=row.stage,
                message=row.message,
                details=row.details_json or {},
                created_at=row.created_at,
            )
            for row in rows
        ]


@router.get("/captures/{capture_id}/events", responses={404: {"model": ErrorDTO}})
async def get_archive_capture_events(
    capture_id: str, since: int = 0
) -> list[ArchiveCaptureEventDTO]:
    result = await run_in_threadpool(_capture_events, capture_id, since)
    if result is None:
        raise HTTPException(status_code=404, detail=_CAPTURE_NOT_FOUND)
    return result


def _cancel_capture(capture_id: str) -> str | None:
    with worker_session_scope() as session:
        repo = WebArchiveRepository(session)
        # Cross-process retry, same reasoning as `_start_capture` above.
        return commit_with_retry(session, lambda: repo.request_cancel(capture_id))


@router.post("/captures/{capture_id}/cancel", responses={404: {"model": ErrorDTO}})
async def cancel_archive_capture(capture_id: str) -> ArchiveCaptureCancelDTO:
    """Cooperative (task-62 §1.6): a `queued` row is finished immediately; a
    `running` one is flagged and stops itself between URLs — this response
    reports which happened, not that the capture is necessarily over yet.
    """
    status = await run_in_threadpool(_cancel_capture, capture_id)
    if status is None:
        raise HTTPException(status_code=404, detail=_CAPTURE_NOT_FOUND)
    return ArchiveCaptureCancelDTO(capture_id=capture_id, status=status)  # type: ignore[arg-type]


def _queue_view() -> ArchiveQueueDTO:
    with worker_session_scope() as session:
        repo = WebArchiveRepository(session)
        running = repo.running_captures()
        queued = repo.queued_captures()
        recent = repo.list_captures(limit=10)
        all_rows = running + queued + recent
        client_names = repo.clients_by_id([r.client_id for r in all_rows])

        def _dto(row: ArchiveCapture, *, position: int | None = None) -> ArchiveCaptureDTO:
            return _capture_dto(
                row, _name_or(client_names, row.client_id),
                target_url=_resolve_target_url(repo, row), queue_position=position,
            )

        return ArchiveQueueDTO(
            running=[_dto(r) for r in running],
            queued=[_dto(r, position=i + 1) for i, r in enumerate(queued)],
            recent=[_dto(r) for r in recent],
        )


@router.get("/queue")
async def get_archive_queue() -> ArchiveQueueDTO:
    return await run_in_threadpool(_queue_view)


register_router(router)
