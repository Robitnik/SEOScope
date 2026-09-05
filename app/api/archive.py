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
    ArchiveCaptureAcceptedDTO,
    ArchiveCaptureDTO,
    ArchiveChangeDiffDTO,
    ArchiveChangeDTO,
    ArchiveChangesPageDTO,
    ArchiveClientCoverageDTO,
    ArchiveLookupDTO,
    ArchiveOverviewDTO,
    ArchiveSnapshotDetailDTO,
    ArchiveSnapshotSummaryDTO,
    ArchiveUrlDTO,
    ArchiveUrlSearchResponseDTO,
    ArchiveUrlSearchResultDTO,
    ArchiveUrlTimelineDTO,
)
from app.webarchive import changes as changesmod
from app.webarchive import rewrite as rewritemod
from app.webarchive import storage
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


def _capture_dto(capture: ArchiveCapture, client_name: str) -> ArchiveCaptureDTO:
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
    )


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
            _capture_dto(c, _name_or(recent_client_names, c.client_id)) for c in recent
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
            return ArchiveLookupDTO(found=False)
        client_name = repo.client_name(archive_url.client_id) or "?"
        return ArchiveLookupDTO(found=True, url=_url_dto(archive_url, client_name))


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
        return [_capture_dto(row, _name_or(client_names, row.client_id)) for row in rows]


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
        # Mirrors `ArchiveScheduler._tick`'s own guard: without this, a
        # manual trigger can start a SECOND capture for a client whose
        # scheduled one is still running (live-observed, task-59
        # 2026-09-04) — wasted duplicate work, not a data-safety issue
        # (cells are isolated), but confusing and worth refusing outright.
        if repo.has_running_capture(client_id):
            return "already_running"
        slot_key = f"manual:{uuid.uuid4()}"
        capture = repo.create_capture(
            client_id, slot_key=slot_key, scheduled_for=datetime.now(UTC), trigger="manual"
        )
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
    return ArchiveCaptureAcceptedDTO(capture_id=result, status_url="/api/v1/archive/captures")


register_router(router)
