"""Site Monitor REST API (task-35 §3.8), prefix `/api/v1`.

Read side never triggers collection — mirrors `analytics.py`/`tech_audit.py`.
Write side ("Check now") only ever enqueues a `queued` `MonitorRun` row; the
worker's `MonitorScheduler` is the sole claimer and executor (same
enqueue/claim split as `SyncJob` — the API process never crawls a client's
site itself).
"""

from __future__ import annotations

import csv
import difflib
import io
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response, StreamingResponse
from openpyxl import Workbook

from app.api import register_router
from app.api.deps import PaginationDep
from app.core.config import get_settings
from app.db.models import (
    MonitorChange,
    MonitorDnsRecord,
    MonitorDnsVersion,
    MonitorPage,
    MonitorPageVersion,
    MonitorRedirect,
    MonitorResource,
    MonitorResourceVersion,
    MonitorRun,
)
from app.db.repositories.clients import ClientRepository
from app.db.repositories.monitor import MonitorRepository
from app.db.session import worker_session_scope
from app.monitor.redirects.collect import process_redirects
from app.monitor.redirects.credentials import (
    CredentialsStatus as CredentialsStatusEnum,
)
from app.monitor.redirects.credentials import (
    credentials_status,
    login_credentials_status,
)
from app.monitor.runner import PageCheckConflict, resolve_base_url, run_single_page_check
from app.monitor.storage import read_blob
from app.schemas.api import ErrorDTO
from app.schemas.monitor import (
    ChangeState,
    MonitorChangeAckRequest,
    MonitorChangeDTO,
    MonitorDnsRecordDTO,
    MonitorDnsVersionDTO,
    MonitorPageDiffDTO,
    MonitorPageDTO,
    MonitorPagesPageDTO,
    MonitorPageVersionDTO,
    MonitorRedirectDTO,
    MonitorRedirectsPageDTO,
    MonitorRedirectsStatusDTO,
    MonitorResourceDiffDTO,
    MonitorResourceDTO,
    MonitorResourceVersionDTO,
    MonitorRunAcceptedDTO,
    MonitorRunDTO,
    MonitorSettingsDTO,
    MonitorSettingsUpdateRequest,
    MonitorSummaryDTO,
    RedirectListState,
    Severity,
    SitemapHealthDTO,
)

router = APIRouter(prefix="/api/v1", tags=["monitor"])

_NOT_FOUND = ErrorDTO(error_code="client_not_found", message="Client not found").model_dump()
_CHANGE_NOT_FOUND = ErrorDTO(error_code="change_not_found", message="Change not found").model_dump()
_PAGE_NOT_FOUND = ErrorDTO(error_code="page_not_found", message="Page not found").model_dump()
_RESOURCE_NOT_FOUND = ErrorDTO(
    error_code="resource_not_found", message="Resource not found"
).model_dump()
_VERSION_NOT_FOUND = ErrorDTO(
    error_code="version_not_found", message="Version not found"
).model_dump()
_DOCUMENT_NOT_FOUND = ErrorDTO(
    error_code="document_not_found", message="Document not found"
).model_dump()
_PAGE_CHECK_CONFLICT = ErrorDTO(
    error_code="page_check_in_progress", message="A check for this page is already running"
).model_dump()


# --- DTO mapping -------------------------------------------------------------


def _run_dto(run: MonitorRun) -> MonitorRunDTO:
    return MonitorRunDTO(
        id=run.id,
        client_id=run.client_id,
        kind=run.kind,  # type: ignore[arg-type]
        trigger=run.trigger,  # type: ignore[arg-type]
        status=run.status,
        started_at=run.started_at,
        finished_at=run.finished_at,
        urls_total=run.urls_total,
        urls_checked=run.urls_checked,
        urls_changed=run.urls_changed,
        urls_new=run.urls_new,
        urls_gone=run.urls_gone,
        requests_made=run.requests_made,
        bytes_downloaded=run.bytes_downloaded,
        changes_critical=run.changes_critical,
        changes_warning=run.changes_warning,
        changes_info=run.changes_info,
        truncated=run.truncated,
        remaining_count=run.remaining_count,
        error_code=run.error_code,
        error_message=run.error_message,
        blocked_count=(run.notes_json or {}).get("blocked_count"),
    )


def _change_dto(change: MonitorChange) -> MonitorChangeDTO:
    return MonitorChangeDTO(
        id=change.id,
        client_id=change.client_id,
        run_id=change.run_id,
        severity=change.severity,  # type: ignore[arg-type]
        change_type=change.change_type,
        scope=change.scope,  # type: ignore[arg-type]
        subject=change.subject,
        related_id=change.related_id,
        old_value=change.old_value,
        new_value=change.new_value,
        details=change.details_json,
        detected_at=change.detected_at,
        acknowledged_at=change.acknowledged_at,
        acknowledged_note=change.acknowledged_note,
        resolved_at=change.resolved_at,
    )


def _page_dto(page: MonitorPage) -> MonitorPageDTO:
    return MonitorPageDTO(
        id=page.id,
        url=page.url,
        is_key_page=page.is_key_page,
        in_sitemap=page.in_sitemap,
        status=page.status,
        final_url=page.final_url,
        indexable=page.indexable,
        meta_robots=page.meta_robots,
        x_robots_tag=page.x_robots_tag,
        title=page.title,
        canonical=page.canonical,
        version_seq=page.version_seq,
        gone_since=page.gone_since,
        consecutive_failures=page.consecutive_failures,
        last_checked_at=page.last_checked_at,
    )


def _page_version_dto(version: MonitorPageVersion) -> MonitorPageVersionDTO:
    return MonitorPageVersionDTO(
        id=version.id,
        observed_at=version.observed_at,
        http_status=version.http_status,
        signals_sha256=version.signals_sha256,
        text_sha256=version.text_sha256,
        raw_sha256=version.raw_sha256,
        body_blob_sha256=version.body_blob_sha256,
        changed_fields=version.changed_fields_json,
        prev_version_id=version.prev_version_id,
    )


def _resource_dto(resource: MonitorResource) -> MonitorResourceDTO:
    structure = resource.structure_json or {}
    entry_count = structure.get("entry_count")
    return MonitorResourceDTO(
        id=resource.id,
        kind=resource.kind,  # type: ignore[arg-type]
        url=resource.url,
        status=resource.status,
        version_seq=resource.version_seq,
        missing_since=resource.missing_since,
        last_checked_at=resource.last_checked_at,
        content_sha256=resource.content_sha256,
        entry_count=entry_count if isinstance(entry_count, int) else None,
        structure_kind=structure.get("kind"),
    )


def _resource_version_dto(version: MonitorResourceVersion) -> MonitorResourceVersionDTO:
    return MonitorResourceVersionDTO(
        id=version.id,
        observed_at=version.observed_at,
        http_status=version.http_status,
        content_sha256=version.content_sha256,
        changed_fields=version.changed_fields_json,
        prev_version_id=version.prev_version_id,
    )


def _dns_record_dto(record: MonitorDnsRecord) -> MonitorDnsRecordDTO:
    return MonitorDnsRecordDTO(
        id=record.id,
        name=record.name,
        record_type=record.record_type,
        values=record.values_json,
        ttl=record.ttl,
        resolver=record.resolver,
        version_seq=record.version_seq,
        last_checked_at=record.last_checked_at,
    )


def _dns_version_dto(version: MonitorDnsVersion) -> MonitorDnsVersionDTO:
    return MonitorDnsVersionDTO(
        id=version.id,
        observed_at=version.observed_at,
        values=version.values_json,
        ttl=version.ttl,
        resolver=version.resolver,
        prev_version_id=version.prev_version_id,
    )


def _redirect_dto(rule: MonitorRedirect) -> MonitorRedirectDTO:
    return MonitorRedirectDTO(
        id=rule.id,
        source=rule.source,  # type: ignore[arg-type]
        source_url=rule.source_url,
        target_url=rule.target_url,
        http_code=rule.http_code,
        match_type=rule.match_type,
        regex=rule.regex,
        status=rule.status,  # type: ignore[arg-type]
        group_name=rule.group_name,
        title=rule.title,
        external_id=rule.external_id,
        first_seen_at=rule.first_seen_at,
        first_seen_window_start=rule.first_seen_window_start,
        last_seen_at=rule.last_seen_at,
        removed_at=rule.removed_at,
        version_seq=rule.version_seq,
    )


# --- summary -----------------------------------------------------------------


def _summary(client_id: str) -> MonitorSummaryDTO | None:
    with worker_session_scope() as session:
        if ClientRepository(session).get(client_id) is None:
            return None
        repo = MonitorRepository(session)
        settings_row = repo.get_settings(client_id)
        last_light = repo.latest_finished_run(client_id, kind="light")
        last_full = repo.latest_finished_run(client_id, kind="full")
        counts = repo.open_counts_by_severity(client_id)
        health = repo.sitemap_health_counts(client_id)
        # Robots resource URL is `{base_url}/robots.txt`; look it up by kind
        # instead of reconstructing the exact URL here.
        robots_resources = repo.list_resources(client_id, kind="robots")
        robots_status = robots_resources[0].status if robots_resources else None

        settings = get_settings()
        next_light_due = None
        next_full_due = None
        if last_light is not None:
            interval = (
                settings_row.light_interval_hours
                if settings_row and settings_row.light_interval_hours
                else settings.monitor_light_interval_hours
            )
            next_light_due = last_light.started_at + timedelta(hours=interval)
        if last_full is not None:
            interval = (
                settings_row.full_interval_hours
                if settings_row and settings_row.full_interval_hours
                else settings.monitor_full_interval_hours
            )
            next_full_due = last_full.started_at + timedelta(hours=interval)

        return MonitorSummaryDTO(
            client_id=client_id,
            enabled=(settings_row.enabled if settings_row else True),
            last_light_run=_run_dto(last_light) if last_light else None,
            last_full_run=_run_dto(last_full) if last_full else None,
            open_critical=counts.get("critical", 0),
            open_warning=counts.get("warning", 0),
            open_info=counts.get("info", 0),
            sitemap_health=SitemapHealthDTO(
                total=health["total"],
                ok=health["ok"],
                redirects=health["redirects"],
                broken=health["broken"],
                unavailable=health["unavailable"],
            ),
            robots_status=robots_status,
            next_light_due_at=next_light_due,
            next_full_due_at=next_full_due,
        )


@router.get(
    "/clients/{client_id}/monitor",
    response_model=MonitorSummaryDTO,
    responses={404: {"model": ErrorDTO}},
)
async def get_monitor_summary(client_id: str) -> MonitorSummaryDTO:
    summary = await run_in_threadpool(_summary, client_id)
    if summary is None:
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    return summary


# --- runs ----------------------------------------------------------------------


def _list_runs(client_id: str, *, kind: str | None, limit: int) -> list[MonitorRunDTO] | None:
    with worker_session_scope() as session:
        if ClientRepository(session).get(client_id) is None:
            return None
        runs = MonitorRepository(session).list_runs(client_id, kind=kind, limit=limit)
        return [_run_dto(r) for r in runs]


@router.get(
    "/clients/{client_id}/monitor/runs",
    response_model=list[MonitorRunDTO],
    responses={404: {"model": ErrorDTO}},
)
async def list_monitor_runs(
    client_id: str, kind: Literal["light", "full"] | None = None, limit: int = 50
) -> list[MonitorRunDTO]:
    runs = await run_in_threadpool(_list_runs, client_id, kind=kind, limit=limit)
    if runs is None:
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    return runs


def _enqueue_run(client_id: str, *, kind: str) -> str | None:
    with worker_session_scope() as session:
        clients = ClientRepository(session)
        if clients.get(client_id) is None:
            return None
        repo = MonitorRepository(session)
        run = repo.enqueue_run(client_id, kind=kind, trigger="manual")
        return run.id


@router.post(
    "/clients/{client_id}/monitor/runs",
    status_code=202,
    response_model=MonitorRunAcceptedDTO,
    responses={404: {"model": ErrorDTO}},
)
async def start_monitor_run(
    client_id: str, kind: Literal["light", "full"] = "light"
) -> MonitorRunAcceptedDTO:
    """ "Check now" — enqueues a run for the worker's scheduler to claim on
    its next tick (≤60s later); progress reads via polling `GET .../runs`.
    """
    run_id = await run_in_threadpool(_enqueue_run, client_id, kind=kind)
    if run_id is None:
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    return MonitorRunAcceptedDTO(
        run_id=run_id, status_url=f"/api/v1/clients/{client_id}/monitor/runs"
    )


# --- changes -------------------------------------------------------------------


def _list_client_changes(
    client_id: str, *, severity: str | None, change_type: str | None, state: ChangeState
) -> list[MonitorChangeDTO] | None:
    with worker_session_scope() as session:
        if ClientRepository(session).get(client_id) is None:
            return None
        changes = MonitorRepository(session).list_changes(
            client_id, severity=severity, change_type=change_type, state=state
        )
        return [_change_dto(c) for c in changes]


@router.get(
    "/clients/{client_id}/monitor/changes",
    response_model=list[MonitorChangeDTO],
    responses={404: {"model": ErrorDTO}},
)
async def list_client_monitor_changes(
    client_id: str,
    severity: Severity | None = None,
    change_type: str | None = None,
    state: ChangeState = "open",
) -> list[MonitorChangeDTO]:
    changes = await run_in_threadpool(
        _list_client_changes, client_id, severity=severity, change_type=change_type, state=state
    )
    if changes is None:
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    return changes


def _list_portfolio_changes(
    *,
    client_id: str | None,
    severity: str | None,
    change_type: str | None,
    state: ChangeState,
    limit: int,
) -> list[MonitorChangeDTO]:
    with worker_session_scope() as session:
        changes = MonitorRepository(session).list_changes(
            client_id, severity=severity, change_type=change_type, state=state, limit=limit
        )
        return [_change_dto(c) for c in changes]


@router.get("/monitor/changes", response_model=list[MonitorChangeDTO])
async def list_portfolio_monitor_changes(
    client_id: str | None = None,
    severity: Severity | None = None,
    change_type: str | None = None,
    state: ChangeState = "open",
    limit: int = 200,
) -> list[MonitorChangeDTO]:
    """Portfolio-wide change feed.

    `client_id` / `change_type` are filtered HERE, not by the caller: `limit`
    takes the newest N rows across every client, so a UI that fetched a window
    and narrowed it in JS would report "no changes" for any client whose rows
    fall outside that window — which is exactly what a busy portfolio does to
    a quiet client (LIVE 2026-08-12). The repository already supported both
    filters; only this endpoint failed to pass them through.
    """
    return await run_in_threadpool(
        _list_portfolio_changes,
        client_id=client_id,
        severity=severity,
        change_type=change_type,
        state=state,
        limit=limit,
    )


def _ack_change(change_id: str, *, note: str | None) -> MonitorChangeDTO | None:
    with worker_session_scope() as session:
        change = MonitorRepository(session).ack_change(change_id, note=note)
        return _change_dto(change) if change else None


@router.post(
    "/monitor/changes/{change_id}/ack",
    response_model=MonitorChangeDTO,
    responses={404: {"model": ErrorDTO}},
)
async def ack_monitor_change(change_id: str, body: MonitorChangeAckRequest) -> MonitorChangeDTO:
    change = await run_in_threadpool(_ack_change, change_id, note=body.note)
    if change is None:
        raise HTTPException(status_code=404, detail=_CHANGE_NOT_FOUND)
    return change


# --- pages -----------------------------------------------------------------


def _list_pages(
    client_id: str,
    *,
    status: str | None,
    indexable: bool | None,
    in_sitemap: bool | None,
    q: str | None,
    sort: str,
    limit: int,
    offset: int,
) -> tuple[list[MonitorPageDTO], int] | None:
    with worker_session_scope() as session:
        if ClientRepository(session).get(client_id) is None:
            return None
        pages, total = MonitorRepository(session).list_pages_page(
            client_id,
            status=status,
            indexable=indexable,
            in_sitemap=in_sitemap,
            q=q,
            sort=sort,
            limit=limit,
            offset=offset,
        )
        return [_page_dto(p) for p in pages], total


@router.get(
    "/clients/{client_id}/monitor/pages",
    response_model=MonitorPagesPageDTO,
    responses={404: {"model": ErrorDTO}},
)
async def list_monitor_pages(
    client_id: str,
    pagination: PaginationDep,
    status: Literal["ok", "broken"] | None = None,
    # task-36: not_indexable / in_sitemap_not200 UI filters — `indexable`/`in_sitemap`
    # were already supported by `list_pages_page` (repository-level) but never
    # threaded through this endpoint. Additive, optional, default-None.
    indexable: bool | None = None,
    in_sitemap: bool | None = None,
    q: str | None = None,
    sort: Literal[
        "last_checked_at",
        "-last_checked_at",
        "status",
        "-status",
        "url",
        "-url",
        "version_seq",
        "-version_seq",
    ] = "last_checked_at",
) -> MonitorPagesPageDTO:
    result = await run_in_threadpool(
        _list_pages,
        client_id,
        status=status,
        indexable=indexable,
        in_sitemap=in_sitemap,
        q=q,
        sort=sort,
        limit=pagination.limit,
        offset=pagination.offset,
    )
    if result is None:
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    pages, total = result
    return MonitorPagesPageDTO(
        pages=pages, total=total, limit=pagination.limit, offset=pagination.offset
    )


def _page_versions(client_id: str, page_id: str) -> list[MonitorPageVersionDTO] | None:
    with worker_session_scope() as session:
        repo = MonitorRepository(session)
        page = repo.get_page_by_id(page_id)
        if page is None or page.client_id != client_id:
            return None
        return [_page_version_dto(v) for v in repo.page_versions(page_id)]


@router.get(
    "/clients/{client_id}/monitor/pages/{page_id}/versions",
    response_model=list[MonitorPageVersionDTO],
    responses={404: {"model": ErrorDTO}},
)
async def list_monitor_page_versions(client_id: str, page_id: str) -> list[MonitorPageVersionDTO]:
    versions = await run_in_threadpool(_page_versions, client_id, page_id)
    if versions is None:
        raise HTTPException(status_code=404, detail=_PAGE_NOT_FOUND)
    return versions


def _page_diff(
    client_id: str, page_id: str, *, from_id: str | None, to_id: str | None
) -> MonitorPageDiffDTO | str:
    with worker_session_scope() as session:
        repo = MonitorRepository(session)
        page = repo.get_page_by_id(page_id)
        if page is None or page.client_id != client_id:
            return "page_not_found"

        versions = repo.page_versions(page_id, limit=2) if not (from_id and to_id) else None
        to_version = repo.get_page_version(to_id) if to_id else (versions[0] if versions else None)
        from_version = (
            repo.get_page_version(from_id)
            if from_id
            else (versions[1] if versions and len(versions) > 1 else None)
        )
        if to_version is None or from_version is None:
            return "version_not_found"

        changed_fields = to_version.changed_fields_json or sorted(
            set(to_version.signals_json) ^ set(from_version.signals_json)
        )

        text_diff: list[str] = []
        settings = get_settings()
        if from_version.body_blob_sha256 and to_version.body_blob_sha256:
            try:
                from_text = read_blob(settings.artifacts_dir, from_version.body_blob_sha256).decode(
                    "utf-8", errors="replace"
                )
                to_text = read_blob(settings.artifacts_dir, to_version.body_blob_sha256).decode(
                    "utf-8", errors="replace"
                )
                text_diff = list(
                    difflib.unified_diff(
                        from_text.splitlines(), to_text.splitlines(), lineterm="", n=2
                    )
                )[:2000]
            except (OSError, PermissionError):
                text_diff = []

        return MonitorPageDiffDTO(
            from_version_id=from_version.id,
            to_version_id=to_version.id,
            changed_fields=list(changed_fields),
            from_signals=from_version.signals_json,
            to_signals=to_version.signals_json,
            text_diff=text_diff,
        )


@router.get(
    "/clients/{client_id}/monitor/pages/{page_id}/diff",
    response_model=MonitorPageDiffDTO,
    responses={404: {"model": ErrorDTO}},
)
async def get_monitor_page_diff(
    client_id: str,
    page_id: str,
    from_version_id: str | None = Query(default=None),
    to_version_id: str | None = Query(default=None),
) -> MonitorPageDiffDTO:
    result = await run_in_threadpool(
        _page_diff, client_id, page_id, from_id=from_version_id, to_id=to_version_id
    )
    if result == "page_not_found":
        raise HTTPException(status_code=404, detail=_PAGE_NOT_FOUND)
    if result == "version_not_found":
        raise HTTPException(status_code=404, detail=_VERSION_NOT_FOUND)
    assert isinstance(result, MonitorPageDiffDTO)
    return result


def _check_page(client_id: str, page_id: str) -> MonitorPageDTO | str:
    settings = get_settings()
    try:
        with worker_session_scope() as session:
            client = ClientRepository(session).get(client_id)
            if client is None:
                return "not_found"
            repo = MonitorRepository(session)
            page = repo.get_page_by_id(page_id)
            if page is None or page.client_id != client_id:
                return "not_found"
            updated = run_single_page_check(
                session, client, page, settings=settings, artifacts_dir=settings.artifacts_dir
            )
            return _page_dto(updated)
    except PageCheckConflict:
        return "conflict"


@router.post(
    "/clients/{client_id}/monitor/pages/{page_id}/check",
    response_model=MonitorPageDTO,
    responses={404: {"model": ErrorDTO}, 409: {"model": ErrorDTO}},
)
async def check_monitor_page(client_id: str, page_id: str) -> MonitorPageDTO:
    """Synchronous re-check of one URL (task-38 §2) — unlike "Check now",
    which enqueues for the worker, this runs inline and returns the updated
    page: an operator confirming a single fix does not want to wait for the
    next scheduler tick or watch a poller.
    """
    result = await run_in_threadpool(_check_page, client_id, page_id)
    if result == "not_found":
        raise HTTPException(status_code=404, detail=_PAGE_NOT_FOUND)
    if result == "conflict":
        raise HTTPException(status_code=409, detail=_PAGE_CHECK_CONFLICT)
    assert isinstance(result, MonitorPageDTO)
    return result


# --- resources (robots/sitemap) -----------------------------------------------


def _resources(client_id: str, kind: str) -> list[MonitorResourceDTO] | None:
    with worker_session_scope() as session:
        if ClientRepository(session).get(client_id) is None:
            return None
        resources = MonitorRepository(session).list_resources(client_id, kind=kind)
        # Index files first, then by URL: a sitemap tree reads top-down, and a
        # flat alphabetical list buries the entry point among its own children.
        return [
            _resource_dto(resource)
            for resource in sorted(
                resources,
                key=lambda r: ((r.structure_json or {}).get("kind") != "index", r.url),
            )
        ]


@router.get(
    "/clients/{client_id}/monitor/resources/{kind}",
    response_model=list[MonitorResourceDTO],
    responses={404: {"model": ErrorDTO}},
)
async def list_monitor_resources(
    client_id: str, kind: Literal["robots", "sitemap", "redirects"]
) -> list[MonitorResourceDTO]:
    """Every tracked robots.txt / sitemap file of one client.

    The versions endpoint below answers "how did THIS file change"; this one
    answers "which files are there at all" — a sitemap tree is routinely a
    dozen files, and until now the UI could only ever reach the first one.
    """
    resources = await run_in_threadpool(_resources, client_id, kind)
    if resources is None:
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    return resources


def _resource_versions(
    client_id: str, kind: str, url: str | None
) -> list[MonitorResourceVersionDTO] | None:
    with worker_session_scope() as session:
        repo = MonitorRepository(session)
        if ClientRepository(session).get(client_id) is None:
            return None
        resources = repo.list_resources(client_id, kind=kind)
        target = next((r for r in resources if url is None or r.url == url), None)
        if target is None:
            return None
        return [_resource_version_dto(v) for v in repo.resource_versions(target.id)]


@router.get(
    "/clients/{client_id}/monitor/resources/{kind}/versions",
    response_model=list[MonitorResourceVersionDTO],
    responses={404: {"model": ErrorDTO}},
)
async def list_monitor_resource_versions(
    client_id: str, kind: Literal["robots", "sitemap", "redirects"], url: str | None = None
) -> list[MonitorResourceVersionDTO]:
    versions = await run_in_threadpool(_resource_versions, client_id, kind, url)
    if versions is None:
        raise HTTPException(status_code=404, detail=_RESOURCE_NOT_FOUND)
    return versions


def _resource_diff(
    client_id: str, kind: str, url: str | None, *, from_id: str | None, to_id: str | None
) -> MonitorResourceDiffDTO | str:
    with worker_session_scope() as session:
        repo = MonitorRepository(session)
        resources = repo.list_resources(client_id, kind=kind)
        target = next((r for r in resources if url is None or r.url == url), None)
        if target is None:
            return "resource_not_found"

        # Default to the two most recent versions; an explicit id picks a
        # specific one out of a wider recent window.
        recent = repo.resource_versions(target.id, limit=200)
        by_id = {v.id: v for v in recent}
        to_version = by_id.get(to_id) if to_id else (recent[0] if recent else None)
        from_version = by_id.get(from_id) if from_id else (recent[1] if len(recent) > 1 else None)
        if to_version is None or from_version is None:
            return "version_not_found"

        return MonitorResourceDiffDTO(
            from_version_id=from_version.id,
            to_version_id=to_version.id,
            changed_fields=to_version.changed_fields_json,
            from_structure=from_version.structure_json,
            to_structure=to_version.structure_json,
        )


@router.get(
    "/clients/{client_id}/monitor/resources/{kind}/diff",
    response_model=MonitorResourceDiffDTO,
    responses={404: {"model": ErrorDTO}},
)
async def get_monitor_resource_diff(
    client_id: str,
    kind: Literal["robots", "sitemap", "redirects"],
    url: str | None = None,
    from_version_id: str | None = Query(default=None),
    to_version_id: str | None = Query(default=None),
) -> MonitorResourceDiffDTO:
    result = await run_in_threadpool(
        _resource_diff, client_id, kind, url, from_id=from_version_id, to_id=to_version_id
    )
    if result == "resource_not_found":
        raise HTTPException(status_code=404, detail=_RESOURCE_NOT_FOUND)
    if result == "version_not_found":
        raise HTTPException(status_code=404, detail=_VERSION_NOT_FOUND)
    assert isinstance(result, MonitorResourceDiffDTO)
    return result


# --- DNS -----------------------------------------------------------------------


def _dns_records(client_id: str) -> list[MonitorDnsRecordDTO] | None:
    with worker_session_scope() as session:
        if ClientRepository(session).get(client_id) is None:
            return None
        records = MonitorRepository(session).all_dns_records(client_id)
        return [_dns_record_dto(r) for r in records]


@router.get(
    "/clients/{client_id}/monitor/dns",
    response_model=list[MonitorDnsRecordDTO],
    responses={404: {"model": ErrorDTO}},
)
async def list_monitor_dns(client_id: str) -> list[MonitorDnsRecordDTO]:
    records = await run_in_threadpool(_dns_records, client_id)
    if records is None:
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    return records


def _dns_record_versions(
    client_id: str, name: str, record_type: str
) -> list[MonitorDnsVersionDTO] | None:
    with worker_session_scope() as session:
        repo = MonitorRepository(session)
        record = repo.get_dns_record(client_id, name, record_type)
        if record is None:
            return None
        return [_dns_version_dto(v) for v in repo.dns_versions(record.id)]


@router.get(
    "/clients/{client_id}/monitor/dns/versions",
    response_model=list[MonitorDnsVersionDTO],
    responses={404: {"model": ErrorDTO}},
)
async def list_monitor_dns_versions(
    client_id: str, name: str, record_type: str
) -> list[MonitorDnsVersionDTO]:
    versions = await run_in_threadpool(_dns_record_versions, client_id, name, record_type)
    if versions is None:
        raise HTTPException(status_code=404, detail=_RESOURCE_NOT_FOUND)
    return versions


# --- redirects (task-51) ------------------------------------------------------


def _list_redirects(
    client_id: str,
    *,
    state: RedirectListState,
    q: str | None,
    sort: str,
    limit: int,
    offset: int,
) -> tuple[list[MonitorRedirectDTO], int] | None:
    with worker_session_scope() as session:
        if ClientRepository(session).get(client_id) is None:
            return None
        rules, total = MonitorRepository(session).list_redirects_page(
            client_id, state=state, q=q, sort=sort, limit=limit, offset=offset
        )
        return [_redirect_dto(r) for r in rules], total


@router.get(
    "/clients/{client_id}/monitor/redirects",
    response_model=MonitorRedirectsPageDTO,
    responses={404: {"model": ErrorDTO}},
)
async def list_monitor_redirects(
    client_id: str,
    pagination: PaginationDep,
    state: RedirectListState = "active",
    q: str | None = None,
    sort: Literal[
        "first_seen_at",
        "-first_seen_at",
        "last_seen_at",
        "-last_seen_at",
        "source_url",
        "-source_url",
    ] = "-first_seen_at",
) -> MonitorRedirectsPageDTO:
    result = await run_in_threadpool(
        _list_redirects,
        client_id,
        state=state,
        q=q,
        sort=sort,
        limit=pagination.limit,
        offset=pagination.offset,
    )
    if result is None:
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    rules, total = result
    return MonitorRedirectsPageDTO(
        redirects=rules, total=total, limit=pagination.limit, offset=pagination.offset
    )


def _combined_credentials_status(settings: Any, client_id: str) -> Any:
    """Application Password first, regular wp-admin login as a fallback —

    same priority `collect._fetch_snapshot` uses (task-51 follow-up): report
    `configured` if EITHER works, `invalid` only when something IS on disk
    but broken with nothing else to fall back on, `missing` only when
    neither credential kind was ever set up for this client.
    """
    app_status = credentials_status(settings.wp_credentials_path, client_id)
    if app_status == CredentialsStatusEnum.configured:
        return app_status
    login_status = login_credentials_status(settings.wp_login_credentials_path, client_id)
    if login_status == CredentialsStatusEnum.configured:
        return login_status
    if CredentialsStatusEnum.invalid in (app_status, login_status):
        return CredentialsStatusEnum.invalid
    return CredentialsStatusEnum.missing


def _redirects_status(client_id: str) -> MonitorRedirectsStatusDTO | None:
    settings = get_settings()
    with worker_session_scope() as session:
        if ClientRepository(session).get(client_id) is None:
            return None
        repo = MonitorRepository(session)
        monitor_settings = repo.get_or_create_settings(client_id)
        session.commit()
        cached = monitor_settings.redirect_source_json or {}
        effective = (
            cached.get("source")
            if monitor_settings.redirect_source == "auto"
            else (
                monitor_settings.redirect_source
                if monitor_settings.redirect_source != "off"
                else None
            )
        )
        summary = repo.redirects_summary(client_id, source=effective)
        last_unavailable = None
        open_unavailable = [
            c
            for c in repo.list_changes(client_id, state="open", limit=50)
            if c.change_type == "redirects_unavailable"
        ]
        if open_unavailable:
            last_unavailable = open_unavailable[0].new_value
        return MonitorRedirectsStatusDTO(
            client_id=client_id,
            redirect_source=monitor_settings.redirect_source,  # type: ignore[arg-type]
            effective_source=effective,  # type: ignore[arg-type]
            credentials_status=_combined_credentials_status(settings, client_id),
            last_checked_at=monitor_settings.redirects_last_checked_at,
            rules_total=summary.get("enabled", 0) + summary.get("disabled", 0),
            rules_enabled=summary.get("enabled", 0),
            rules_disabled=summary.get("disabled", 0),
            last_unavailable_reason=last_unavailable,
        )


@router.get(
    "/clients/{client_id}/monitor/redirects/status",
    response_model=MonitorRedirectsStatusDTO,
    responses={404: {"model": ErrorDTO}},
)
async def get_monitor_redirects_status(client_id: str) -> MonitorRedirectsStatusDTO:
    result = await run_in_threadpool(_redirects_status, client_id)
    if result is None:
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    return result


def _check_redirects(client_id: str) -> str | None:
    settings = get_settings()
    with worker_session_scope() as session:
        client = ClientRepository(session).get(client_id)
        if client is None:
            return None
        repo = MonitorRepository(session)
        monitor_settings = repo.get_or_create_settings(client_id)
        base_url = resolve_base_url(
            client, monitor_settings.base_url_override, monitor_settings.canonical_www
        )
        # A real (if minimal) `MonitorRun` row, same as a single-page "Check
        # now" (task-38 §2) — every `MonitorChange` needs a legitimate
        # `run_id`, and the operator gets this in "Check history" too.
        run = repo.create_run(client_id, kind="light", trigger="manual")
        session.commit()
        process_redirects(
            repo,
            client,
            base_url,
            run_id=run.id,
            monitor_settings=monitor_settings,
            settings=settings,
            artifacts_dir=settings.artifacts_dir,
            key_pages=set(),
            now=datetime.now(UTC),
        )
        repo.finish_run(run.id, status="succeeded", truncated=False)
        session.commit()
        return "ok"


@router.post(
    "/clients/{client_id}/monitor/redirects/check",
    response_model=MonitorRedirectsStatusDTO,
    responses={404: {"model": ErrorDTO}},
)
async def check_monitor_redirects(client_id: str) -> MonitorRedirectsStatusDTO:
    """Synchronous re-check (mirrors `check_monitor_page`, task-38 §2) —

    runs inline against the client's site right now instead of waiting for
    the next scheduled light/full run, then returns the same status shape
    `GET .../redirects/status` does.
    """
    result = await run_in_threadpool(_check_redirects, client_id)
    if result is None:
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    status = await run_in_threadpool(_redirects_status, client_id)
    assert status is not None
    return status


# --- documents (stored robots.txt / page bodies) ------------------------------


def _document(sha256: str) -> tuple[bytes, str | None] | None:
    settings = get_settings()
    with worker_session_scope() as session:
        blob = MonitorRepository(session).get_blob(sha256)
    if blob is None:
        return None
    try:
        data = read_blob(settings.artifacts_dir, sha256)
    except (OSError, PermissionError):
        return None
    return data, blob.mime


@router.get("/monitor/documents/{sha256}", responses={404: {"model": ErrorDTO}})
async def get_monitor_document(sha256: str) -> Response:
    result = await run_in_threadpool(_document, sha256)
    if result is None:
        raise HTTPException(status_code=404, detail=_DOCUMENT_NOT_FOUND)
    data, mime = result
    return Response(content=data, media_type=mime or "text/plain")


# --- settings --------------------------------------------------------------


def _settings_dto(client_id: str) -> MonitorSettingsDTO | None:
    with worker_session_scope() as session:
        if ClientRepository(session).get(client_id) is None:
            return None
        settings = MonitorRepository(session).get_or_create_settings(client_id)
        session.commit()
        return MonitorSettingsDTO(
            client_id=settings.client_id,
            enabled=settings.enabled,
            base_url_override=settings.base_url_override,
            canonical_www=settings.canonical_www,
            light_interval_hours=settings.light_interval_hours,
            full_interval_hours=settings.full_interval_hours,
            max_urls=settings.max_urls,
            monitor_when_paused=settings.monitor_when_paused,
            ignore_patterns=settings.ignore_patterns_json,
            muted_change_types=settings.muted_change_types_json,
            fetch_mode=settings.fetch_mode,  # type: ignore[arg-type]
            blocked_detected_at=settings.blocked_detected_at,
            updated_at=settings.updated_at,
        )


@router.get(
    "/clients/{client_id}/monitor/settings",
    response_model=MonitorSettingsDTO,
    responses={404: {"model": ErrorDTO}},
)
async def get_monitor_settings(client_id: str) -> MonitorSettingsDTO:
    settings = await run_in_threadpool(_settings_dto, client_id)
    if settings is None:
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    return settings


def _update_settings(client_id: str, fields: dict[str, Any]) -> MonitorSettingsDTO | None:
    with worker_session_scope() as session:
        if ClientRepository(session).get(client_id) is None:
            return None
        repo = MonitorRepository(session)
        mapped = dict(fields)
        if "ignore_patterns" in mapped:
            mapped["ignore_patterns_json"] = mapped.pop("ignore_patterns")
        if "muted_change_types" in mapped:
            mapped["muted_change_types_json"] = mapped.pop("muted_change_types")
        settings = repo.update_settings(client_id, **mapped)
        return MonitorSettingsDTO(
            client_id=settings.client_id,
            enabled=settings.enabled,
            base_url_override=settings.base_url_override,
            canonical_www=settings.canonical_www,
            light_interval_hours=settings.light_interval_hours,
            full_interval_hours=settings.full_interval_hours,
            max_urls=settings.max_urls,
            monitor_when_paused=settings.monitor_when_paused,
            ignore_patterns=settings.ignore_patterns_json,
            muted_change_types=settings.muted_change_types_json,
            fetch_mode=settings.fetch_mode,  # type: ignore[arg-type]
            blocked_detected_at=settings.blocked_detected_at,
            updated_at=settings.updated_at,
        )


@router.patch(
    "/clients/{client_id}/monitor/settings",
    response_model=MonitorSettingsDTO,
    responses={404: {"model": ErrorDTO}},
)
async def update_monitor_settings(
    client_id: str, body: MonitorSettingsUpdateRequest
) -> MonitorSettingsDTO:
    fields = body.model_dump(exclude_unset=True)
    result = await run_in_threadpool(_update_settings, client_id, fields)
    if result is None:
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    return result


# --- export ------------------------------------------------------------------

_EXPORT_HEADER = (
    "URL",
    "Status",
    "Indexable",
    # The raw directive travels with the export too: "Indexable: no" alone does
    # not say whether it was `noindex, follow` or `noindex, nofollow`, and the
    # export is what gets sent to the client's developer to act on.
    "Meta robots",
    "X-Robots-Tag",
    "In sitemap",
    "Last checked",
    "Title",
)


def _export_rows(client_id: str) -> tuple[list[MonitorPage], str] | str:
    with worker_session_scope() as session:
        client = ClientRepository(session).get(client_id)
        if client is None:
            return "client_not_found"
        pages = MonitorRepository(session).all_pages(client_id)
        slug = client.name.upper().replace(" ", "-").replace("/", "-")
        return sorted(pages, key=lambda p: p.url), slug


def _page_row(page: MonitorPage) -> tuple[str, str, str, str, str, str, str, str]:
    return (
        page.url,
        str(page.status) if page.status is not None else "",
        "yes" if page.indexable else ("no" if page.indexable is False else ""),
        page.meta_robots or "",
        page.x_robots_tag or "",
        "yes" if page.in_sitemap else "no",
        page.last_checked_at.isoformat() if page.last_checked_at else "",
        page.title or "",
    )


@router.get("/clients/{client_id}/monitor/export.csv", responses={404: {"model": ErrorDTO}})
async def export_monitor_csv(client_id: str) -> StreamingResponse:
    result = await run_in_threadpool(_export_rows, client_id)
    if result == "client_not_found":
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    assert not isinstance(result, str)
    pages, slug = result

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(_EXPORT_HEADER)
    for page in pages:
        writer.writerow(_page_row(page))
    buffer.seek(0)

    filename = f"{slug}_Monitor_{date.today().strftime('%Y%m%d')}.csv"
    return StreamingResponse(
        iter([buffer.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/clients/{client_id}/monitor/export.xlsx", responses={404: {"model": ErrorDTO}})
async def export_monitor_xlsx(client_id: str) -> StreamingResponse:
    result = await run_in_threadpool(_export_rows, client_id)
    if result == "client_not_found":
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    assert not isinstance(result, str)
    pages, slug = result

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Monitor pages"
    sheet.append(_EXPORT_HEADER)
    for page in pages:
        sheet.append(_page_row(page))
    for column_cells in sheet.columns:
        length = max((len(str(c.value)) for c in column_cells if c.value is not None), default=10)
        sheet.column_dimensions[column_cells[0].column_letter].width = min(length + 2, 80)

    buffer = io.BytesIO()
    workbook.save(buffer)
    buffer.seek(0)

    filename = f"{slug}_Monitor_{date.today().strftime('%Y%m%d')}.xlsx"
    return StreamingResponse(
        iter([buffer.getvalue()]),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


register_router(router)
