"""Reports engine REST API (task-42 §5), prefix `/api/v1/reports`.

Read side never triggers generation — mirrors `app/api/monitor.py`. `POST
/runs` only ever enqueues a `queued` `ReportRun` row; the worker's own
`ReportScheduler` (task-42 §2.3) is the sole claimer/executor, the same
enqueue/claim split `SyncJob`/`MonitorRun` already use — this API process
never opens a browser or calls the Reporting Ninja API itself.
"""

from __future__ import annotations

import hashlib
import io
import os
import tempfile
import zipfile
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from sqlalchemy.orm import Session

from app.api import register_router
from app.api.deps import PaginationDep
from app.core.config import get_settings
from app.core.security import PathTraversalError, safe_resolve
from app.db.models import ReportBatch, ReportRun
from app.db.repositories.clients import ClientRepository
from app.db.repositories.reports import ReportRepository
from app.db.session import worker_session_scope
from app.jobs.events import event_stream
from app.reports import ninja_browser
from app.reports.batch import (
    NoClientsSelectedError,
    cancel_batch,
    create_batch,
    latest_runs_by_client,
    rebuild_run,
)
from app.reports.batch_pdf import NoClientPdfsError, build_portfolio_pdf
from app.reports.batch_xlsx import NoClientDataError, build_portfolio_xlsx
from app.reports.client_pdf import client_pdf_filename, render_client_pdf
from app.reports.document import render_document_html
from app.reports.period_retry import retry_period
from app.reports.periods import InvalidComparatorMonthsError, InvalidPeriodError
from app.reports.runner import build_periods, period_rows
from app.reports.table import ReportTable
from app.reports.verify import DIFFER_VERDICTS
from app.schemas.api import ErrorDTO
from app.schemas.enums import Provider
from app.schemas.reports import (
    AccuracyClientRowDTO,
    AccuracyReportDTO,
    BatchClientRetryRequest,
    BatchPortfolioTableDTO,
    BatchPortfolioTableRowDTO,
    BindingStatus,
    CrossCheckStatus,
    NinjaBindingRowDTO,
    NinjaBindingsDTO,
    NinjaBindingState,
    NinjaReportCandidateDTO,
    NinjaReportNameDTO,
    NinjaReportNameUpdateRequest,
    ReportBatchAcceptedDTO,
    ReportBatchClientRowDTO,
    ReportBatchCreateRequest,
    ReportBatchDTO,
    ReportBatchesPageDTO,
    ReportFileDTO,
    ReportHistoryDTO,
    ReportRunAcceptedDTO,
    ReportRunDTO,
    ReportRunPeriodDTO,
    ReportRunRequest,
    ReportRunsPageDTO,
    ReportTableDTO,
    ReportTablePeriodDTO,
    ReportTableRowDTO,
    ReportTypeDTO,
    ReportTypeEstimateDTO,
    ReportTypesDTO,
    RunRebuildRequest,
)

router = APIRouter(prefix="/api/v1/reports", tags=["reports"])

_CLIENT_NOT_FOUND = ErrorDTO(error_code="client_not_found", message="Client not found").model_dump()
_RUN_NOT_FOUND = ErrorDTO(error_code="run_not_found", message="Report run not found").model_dump()
_FILE_NOT_FOUND = ErrorDTO(error_code="file_not_found", message="File not found").model_dump()
_RUN_NOT_CANCELLABLE = ErrorDTO(
    error_code="run_not_cancellable", message="This run has already finished"
).model_dump()
_RUN_NOT_FINISHED = ErrorDTO(
    error_code="run_not_finished", message="This report has not finished yet"
).model_dump()
_BATCH_NOT_FOUND = ErrorDTO(
    error_code="batch_not_found", message="Report batch not found"
).model_dump()
_BATCH_NOT_CANCELLABLE = ErrorDTO(
    error_code="batch_not_cancellable", message="This batch has already finished"
).model_dump()
_PERIOD_NOT_FOUND = ErrorDTO(
    error_code="period_not_found", message="No such period on this run"
).model_dump()
_PERIOD_NOT_RETRYABLE = ErrorDTO(
    error_code="period_not_retryable", message="This period is not in a failed state"
).model_dump()
_NO_CLIENTS_SELECTED = ErrorDTO(
    error_code="no_clients_selected", message="At least one client must be selected"
).model_dump()
_RUN_NOT_RETRYABLE = ErrorDTO(
    error_code="run_not_retryable", message="This run has not finished yet"
).model_dump()
_CLIENT_NOT_IN_BATCH = ErrorDTO(
    error_code="client_not_in_batch", message="This client has no run in this batch"
).model_dump()
_CLIENT_NOT_RETRYABLE = ErrorDTO(
    error_code="client_not_retryable", message="This client's run has not finished yet"
).model_dump()

_TERMINAL_STATUSES = ("succeeded", "partial", "failed", "cancelled")

# Measured live (task-42 §1.2): ~2s/column for the API path, ~55s/column for
# the PDF path — roughly constant per column across all 4 kinds, so one
# estimate serves the whole catalog; the caller multiplies by column count.
_ESTIMATE = ReportTypeEstimateDTO(api_seconds_per_column=2.0, pdf_seconds_per_column=55.0)
_TYPES = [
    ReportTypeDTO(kind="monthly", label="Monthly", estimate=_ESTIMATE),
    ReportTypeDTO(kind="yearly", label="Yearly", estimate=_ESTIMATE),
    ReportTypeDTO(kind="custom_months", label="Custom", estimate=_ESTIMATE),
    ReportTypeDTO(kind="comparator", label="Comparator", estimate=_ESTIMATE),
]


# --- DTO mapping -------------------------------------------------------------


def _run_dto(run: ReportRun, *, ninja_report_name: str | None = None) -> ReportRunDTO:
    return ReportRunDTO(
        id=run.id,
        client_id=run.client_id,
        kind=run.kind,  # type: ignore[arg-type]
        source=run.source,  # type: ignore[arg-type]
        status=run.status,  # type: ignore[arg-type]
        period_count=run.period_count,
        periods_done=run.periods_done,
        periods_failed=run.periods_failed,
        cross_check_status=run.cross_check_status,  # type: ignore[arg-type]
        error_code=run.error_code,
        error_message=run.error_message,
        cancel_requested=run.cancel_requested,
        params=dict(run.params_json or {}),
        periods=[ReportRunPeriodDTO(**period) for period in (run.periods_json or [])],
        ninja_report_name=ninja_report_name,
        batch_id=run.batch_id,
        queued_at=run.queued_at,
        started_at=run.started_at,
        finished_at=run.finished_at,
    )


# --- GET /types ---------------------------------------------------------------


@router.get("/types")
async def get_report_types() -> ReportTypesDTO:
    settings = get_settings()
    return ReportTypesDTO(
        types=_TYPES,
        max_months=settings.reports_max_months,
        max_comparator_years=settings.reports_max_comparator_years,
    )


# --- GET/POST /runs ------------------------------------------------------------


def _list_runs(
    *, client_id: str | None, kind: str | None, source: str | None, status: str | None,
    has_batch: bool | None, limit: int, offset: int,
) -> tuple[list[ReportRunDTO], int]:
    with worker_session_scope() as session:
        rows, total = ReportRepository(session).list_runs(
            client_id=client_id, kind=kind, source=source, status=status,
            has_batch=has_batch, limit=limit, offset=offset,
        )
        return [_run_dto(r) for r in rows], total


@router.get("/runs")
async def list_report_runs(
    pagination: PaginationDep,
    client_id: str | None = None,
    kind: str | None = None,
    source: str | None = None,
    status: str | None = None,
    # `Scope` filter (task-47 §3.5 point 6): omitted/`None` -> every run
    # (matches task-42/43/45 behaviour unchanged); `false` -> `Single runs`
    # only, hiding batch children so a portfolio-wide batch doesn't drown the
    # history in near-identical rows. `Batches` itself is `GET /batches`.
    has_batch: bool | None = None,
) -> ReportRunsPageDTO:
    rows, total = await run_in_threadpool(
        _list_runs, client_id=client_id, kind=kind, source=source, status=status,
        has_batch=has_batch, limit=pagination.limit, offset=pagination.offset,
    )
    return ReportRunsPageDTO(
        rows=rows, total=total, limit=pagination.limit, offset=pagination.offset
    )


def _params_for(body: ReportRunRequest) -> dict[str, Any]:
    params: dict[str, Any] = {"force_refresh": body.force_refresh}
    if body.kind == "yearly":
        params["years"] = body.years or []
        params["span"] = body.span or "full"
    if body.kind in ("custom_months", "comparator"):
        params["months"] = body.months or []
    return params


def _create_run(body: ReportRunRequest) -> str:
    """Raises `HTTPException` for every validated failure — 404 for a
    missing client, 422 for everything else (task-42 §5). Only reaches the
    scheduler's queue once every preflight in §3.4/§3.8 has passed.
    """
    settings = get_settings()
    params = _params_for(body)
    try:
        periods = build_periods(
            body.kind, params,
            max_months=settings.reports_max_months,
            max_comparator_years=settings.reports_max_comparator_years,
        )
    except InvalidComparatorMonthsError as exc:
        raise HTTPException(
            422, ErrorDTO(error_code="invalid_comparator_months", message=str(exc)).model_dump()
        ) from exc
    except (InvalidPeriodError, ValueError) as exc:
        raise HTTPException(
            422, ErrorDTO(error_code="invalid_period", message=str(exc)).model_dump()
        ) from exc

    with worker_session_scope() as session:
        client_repo = ClientRepository(session)
        client = client_repo.get(body.client_id)
        if client is None:
            raise HTTPException(404, _CLIENT_NOT_FOUND)

        rn_integration = client_repo.get_integration(body.client_id, Provider.reporting_ninja)
        if rn_integration is None or not rn_integration.config_json.get("connection_key"):
            raise HTTPException(
                422,
                ErrorDTO(
                    error_code="not_configured",
                    message="Reporting Ninja is not configured for this client",
                ).model_dump(),
            )
        if body.source == "pdf" and not rn_integration.config_json.get("ninja_report_name"):
            raise HTTPException(
                422,
                ErrorDTO(
                    error_code="report_name_missing",
                    message="No Reporting Ninja report name saved for this client",
                ).model_dump(),
            )

        run = ReportRepository(session).create_run(
            body.client_id, kind=body.kind, source=body.source,
            params=params, period_count=len(periods),
            # The period list is already known here (it was just built for
            # validation) — persisting it now means a `queued` run's UI shows
            # every period with its real date range before the worker even
            # claims it (task-45 §2.1), instead of `Period 1 … Period 6`.
            periods=period_rows(periods),
        )
        return run.id


@router.post(
    "/runs", status_code=202, responses={404: {"model": ErrorDTO}, 422: {"model": ErrorDTO}}
)
async def create_report_run(body: ReportRunRequest) -> ReportRunAcceptedDTO:
    run_id = await run_in_threadpool(_create_run, body)
    return ReportRunAcceptedDTO(
        run_id=run_id,
        status_url=f"/api/v1/reports/runs/{run_id}",
        events_url=f"/api/v1/reports/runs/{run_id}/events",
    )


def _get_run(run_id: str) -> ReportRunDTO | None:
    with worker_session_scope() as session:
        run = ReportRepository(session).get(run_id)
        if run is None:
            return None
        # The client's currently bound Reporting Ninja report, so a wrong
        # binding is visible in the run header rather than only in the
        # database (task-45 §2.2). Single-run reads only — the list endpoint
        # would need one lookup per row for a header it never renders.
        integration = ClientRepository(session).get_integration(
            run.client_id, Provider.reporting_ninja
        )
        name = integration.config_json.get("ninja_report_name") if integration else None
        return _run_dto(run, ninja_report_name=name)


@router.get("/runs/{run_id}", responses={404: {"model": ErrorDTO}})
async def get_report_run(run_id: str) -> ReportRunDTO:
    dto = await run_in_threadpool(_get_run, run_id)
    if dto is None:
        raise HTTPException(404, _RUN_NOT_FOUND)
    return dto


# --- GET /runs/{id}/events (SSE) ---------------------------------------------


class _SseEvent:
    """Adapts a `ReportRunEvent` row to what `app.jobs.events._format_sse`
    reads (`.level`/`.stage`/`.percent`/`.message`/`.details_json`/
    `.timestamp`/`.seq`; `.provider` is looked up via `getattr(..., None)`
    there, but `.percent` is read directly, so it must exist here too) —
    read-only reuse of the shared SSE plumbing without adding `provider`/
    `percent` columns reports have no use for.
    """

    def __init__(
        self, seq: int, level: str, stage: str | None, message: str,
        details_json: Any, timestamp: Any,
    ):
        self.seq = seq
        self.level = level
        self.stage = stage
        self.percent: float | None = None
        self.message = message
        self.details_json = details_json
        self.timestamp = timestamp


def _fetch_events(run_id: str, last_seq: int) -> list[_SseEvent]:
    with worker_session_scope() as session:
        rows = ReportRepository(session).events_since(run_id, last_seq)
        return [
            _SseEvent(r.seq, r.level, r.stage, r.message, r.details_json, r.created_at)
            for r in rows
        ]


def _run_is_done(run_id: str) -> bool:
    with worker_session_scope() as session:
        run = ReportRepository(session).get(run_id)
        return run is None or run.status in _TERMINAL_STATUSES


@router.get("/runs/{run_id}/events")
async def report_run_events(run_id: str, request: Request) -> StreamingResponse:
    header = request.headers.get("last-event-id") or request.query_params.get("last_event_id")
    last_event_id = int(header) if header else 0

    async def fetch_since(last_seq: int) -> list[Any]:
        return await run_in_threadpool(_fetch_events, run_id, last_seq)

    async def is_done() -> bool:
        return await run_in_threadpool(_run_is_done, run_id)

    return StreamingResponse(
        event_stream(fetch_since, last_event_id=last_event_id, is_done=is_done),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# --- POST /runs/{id}/cancel ---------------------------------------------------


def _cancel(run_id: str) -> str | None:
    with worker_session_scope() as session:
        repo = ReportRepository(session)
        if repo.get(run_id) is None:
            return "run_not_found"
        if repo.request_cancel(run_id) is None:
            return "run_not_cancellable"
        return None


@router.post(
    "/runs/{run_id}/cancel", responses={404: {"model": ErrorDTO}, 409: {"model": ErrorDTO}}
)
async def cancel_report_run(run_id: str) -> ReportRunDTO:
    error = await run_in_threadpool(_cancel, run_id)
    if error == "run_not_found":
        raise HTTPException(404, _RUN_NOT_FOUND)
    if error == "run_not_cancellable":
        raise HTTPException(409, _RUN_NOT_CANCELLABLE)
    dto = await run_in_threadpool(_get_run, run_id)
    if dto is None:  # pragma: no cover - can't disappear between the two calls above
        raise HTTPException(404, _RUN_NOT_FOUND)
    return dto


# --- POST /runs/{id}/rebuild (task-49 §2.5) ---------------------------------
#
# The whole-run "Retry" button used to call `POST /runs` with the source
# run's own params spread in, silently keeping `force_refresh: false` even
# when an operator retried BECAUSE the data was wrong (task-49 §1.4/§2.4
# point 3). This makes `force_refresh` an explicit choice every time, and —
# unlike the old behaviour — preserves the source run's `batch_id`, so a
# rebuilt batch child stays inside its batch instead of becoming an orphan.


def _rebuild_run(run_id: str, *, force_refresh: bool) -> tuple[str | None, str | None]:
    settings = get_settings()
    return rebuild_run(
        worker_session_scope, run_id, force_refresh=force_refresh,
        max_months=settings.reports_max_months,
        max_comparator_years=settings.reports_max_comparator_years,
    )


@router.post(
    "/runs/{run_id}/rebuild",
    status_code=202,
    responses={404: {"model": ErrorDTO}, 409: {"model": ErrorDTO}},
)
async def rebuild_report_run(run_id: str, body: RunRebuildRequest) -> ReportRunAcceptedDTO:
    error, new_run_id = await run_in_threadpool(
        _rebuild_run, run_id, force_refresh=body.force_refresh
    )
    if error == "run_not_found":
        raise HTTPException(404, _RUN_NOT_FOUND)
    if error == "run_not_retryable":
        raise HTTPException(409, _RUN_NOT_RETRYABLE)
    if new_run_id is None:  # pragma: no cover - exhaustive with the checks above
        raise HTTPException(404, _RUN_NOT_FOUND)
    return ReportRunAcceptedDTO(
        run_id=new_run_id,
        status_url=f"/api/v1/reports/runs/{new_run_id}",
        events_url=f"/api/v1/reports/runs/{new_run_id}/events",
    )


# --- GET /runs/{id}/table ------------------------------------------------------


def _get_table(run_id: str) -> ReportTableDTO | None:
    with worker_session_scope() as session:
        run = ReportRepository(session).get(run_id)
    if run is None or run.result_json is None:
        return None
    data = run.result_json
    return ReportTableDTO(
        run_id=run.id,
        kind=run.kind,  # type: ignore[arg-type]
        source=run.source,  # type: ignore[arg-type]
        periods=[ReportTablePeriodDTO(**p) for p in data.get("periods", [])],
        change_labels=data.get("change_labels", []),
        rows=[ReportTableRowDTO(**r) for r in data.get("rows", [])],
        sources=data.get("sources", {}),
        cross_check=data.get("cross_check"),
        history=ReportHistoryDTO(**data["history"]) if data.get("history") else None,
        cached_periods=data.get("cached_periods", []),
        oldest_cached_at=(
            date.fromisoformat(data["oldest_cached_at"]) if data.get("oldest_cached_at") else None
        ),
    )


@router.get("/runs/{run_id}/table", responses={404: {"model": ErrorDTO}})
async def get_report_table(run_id: str) -> ReportTableDTO:
    dto = await run_in_threadpool(_get_table, run_id)
    if dto is None:
        raise HTTPException(404, _RUN_NOT_FOUND)
    return dto


# --- GET /runs/{id}/files -------------------------------------------------------
#
# Additive, task-43: the finished-run view (§4 "Files") needs a filename/size
# per PDF/XLSX/screenshot to render `View`/`Download` links and the history
# table's `6 PDF · 1 XLSX` count — `ReportRepository.list_files_for_run` has
# existed since task-42, it just had no HTTP route. `app/api/reports.py` is
# task-42's owned path and task-43's plan lists it only as adjacent, but this
# one read-only, purely-additive route is the only way the UI can reach data
# task-42 already persists; documented in task-43's Completion report.


def _list_files(run_id: str) -> list[ReportFileDTO] | None:
    with worker_session_scope() as session:
        repo = ReportRepository(session)
        if repo.get(run_id) is None:
            return None
        files = repo.list_files_for_run(run_id)
        return [
            ReportFileDTO(
                id=f.id, run_id=f.run_id, kind=f.kind,  # type: ignore[arg-type]
                period_label=f.period_label, date_from=f.date_from, date_to=f.date_to,
                is_partial_period=f.is_partial_period, size_bytes=f.size_bytes, mime=f.mime,
                created_at=f.created_at,
            )
            for f in files
        ]


@router.get("/runs/{run_id}/files", responses={404: {"model": ErrorDTO}})
async def list_report_run_files(run_id: str) -> list[ReportFileDTO]:
    dtos = await run_in_threadpool(_list_files, run_id)
    if dtos is None:
        raise HTTPException(404, _RUN_NOT_FOUND)
    return dtos


# --- GET /files/{id} -----------------------------------------------------------


def _fetch_file(file_id: str) -> tuple[str | None, Path] | None:
    settings = get_settings()
    with worker_session_scope() as session:
        file = ReportRepository(session).get_file(file_id)
        if file is None:
            return None
        resolved = safe_resolve(settings.artifacts_dir, file.path)
        return file.mime, resolved


@router.get("/files/{file_id}", responses={404: {"model": ErrorDTO}})
async def download_report_file(
    file_id: str,
    disposition: str = Query("attachment", pattern="^(inline|attachment)$"),
) -> FileResponse:
    try:
        found = await run_in_threadpool(_fetch_file, file_id)
    except PathTraversalError:
        found = None
    if found is None:
        raise HTTPException(404, _FILE_NOT_FOUND)
    mime, resolved = found
    if not resolved.is_file():
        raise HTTPException(404, _FILE_NOT_FOUND)
    headers: dict[str, str] = {}
    if disposition == "inline":
        # `SecurityHeadersMiddleware` (app/core/security.py) sets a blanket
        # `X-Frame-Options: DENY` via `setdefault` — it never overwrites a
        # header the route already set, but it DOES clobber this response by
        # default otherwise, which made the `?disposition=inline` PDF viewer
        # (task-43 §4) render nothing: the browser fetches the PDF fine, then
        # refuses to paint it inside the `<iframe>` at all because of that
        # header (found live, real client PDF, "View" showing a broken-image
        # placeholder). Same restrictive `default-src`/`img-src` as the
        # blanket policy, `frame-ancestors 'self'` added so modern browsers
        # (which prefer CSP over X-Frame-Options when both are present) allow
        # same-origin framing for this one response only — `attachment`
        # downloads are never framed, so they keep the default `DENY`.
        headers["X-Frame-Options"] = "SAMEORIGIN"
        headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data: https: http:; frame-ancestors 'self'"
        )
    return FileResponse(
        resolved,
        media_type=mime or "application/octet-stream",
        filename=resolved.name,
        content_disposition_type=disposition,
        headers=headers or None,
    )


# --- GET/PUT /clients/{id}/ninja-report ---------------------------------------


def _name_matches_client(report_name: str, client_name: str) -> bool:
    return report_name.upper().startswith(client_name.upper())


def _cabinet_has_report_name(name: str) -> bool | None:
    """Does the Reporting Ninja cabinet actually hold this exact report name?

    Asked through the cabinet's OWN search (task-48 §2.1 point 1), never
    through the scroll-until-stable full list. Measured live on this account
    (2026-08-19, task-49): `list_report_names` returned 16 names ending at
    `GRP-Internal report` while reporting `truncated=False` — a silently
    INCOMPLETE list claiming to be complete, because "the visible set stopped
    growing" cannot tell a finished scroll from a stalled one. Validating
    against it rejected every real client sorting past "G" (SWM, SAPHNY, SDS,
    SHB, HWELL, LAXTON, NYCHA, PJC) as `report_name_unknown` — i.e. exactly
    the clients task-49's inline Connect exists to fix, and the same
    past-the-cutoff trap task-48 §1.6 already found once.

    Search has no such failure mode: the server does the filtering, so
    virtualization cannot hide a row from it, and an empty result for one
    exact name IS evidence of absence (unlike an unproven absence from a
    truncated scroll, task-48 §1.7). `None` means the question could not be
    asked at all — no credentials, hence no browser — never "absent".
    """
    email = os.environ.get("REPORTING_NINJA_EMAIL", "")
    password = os.environ.get("REPORTING_NINJA_PASSWORD", "")
    if not email or not password:
        return None
    settings = get_settings()
    found = ninja_browser.search_report_names(
        email=email, password=password, headless=settings.reports_headless, query=name
    )
    return name in found


def _client_identity(client_id: str) -> tuple[str, str | None, bool] | None:
    """`(client_name, saved_report_name, has_integration)` or `None`."""
    with worker_session_scope() as session:
        client_repo = ClientRepository(session)
        client = client_repo.get(client_id)
        if client is None:
            return None
        integration = client_repo.get_integration(client_id, Provider.reporting_ninja)
        saved = integration.config_json.get("ninja_report_name") if integration else None
        return client.name, saved, integration is not None


def _current_ninja_report(client_id: str) -> NinjaReportNameDTO | None:
    identity = _client_identity(client_id)
    if identity is None:
        return None
    client_name, current, _has_integration = identity
    return NinjaReportNameDTO(
        client_id=client_id, client_name=client_name, ninja_report_name=current
    )


def _discover_ninja_report(client_id: str) -> NinjaReportNameDTO | None:
    """task-45 §1.2: the account list is 32 reports belonging to ~30 different
    clients, and task-42 returned all of them as one `candidates` list where
    `exact_match` only affected SORT ORDER. Nothing stopped an operator from
    picking `EFFYS-Internal report` for SAPHNY and saving it forever, after
    which every PDF run parsed a foreign report's 8/8 metrics under SAPHNY's
    name. Splitting the answer is the first of three guards (the confirmation
    checkbox and the server-side `PUT` validation are the other two).

    task-48 §1.6/§2.1: a plain read of the reports table only ever saw
    whatever the virtualized DOM had painted (measured live: 32 names,
    cutting off before every client sorting after "G"). `matches` now comes
    from the cabinet's own search — the same mechanism `_open_report`
    already uses to open a report by name — so a client past that cutoff is
    still found; `others`/`others_truncated` come from a bounded
    scroll-until-stable pass, done in the same browser session.
    """
    identity = _client_identity(client_id)
    if identity is None:
        return None
    client_name, current, _has_integration = identity

    email = os.environ.get("REPORTING_NINJA_EMAIL", "")
    password = os.environ.get("REPORTING_NINJA_PASSWORD", "")
    if not email or not password:
        return NinjaReportNameDTO(
            client_id=client_id, client_name=client_name, ninja_report_name=current
        )

    settings = get_settings()
    discovery = ninja_browser.discover_report_names(
        email=email, password=password, headless=settings.reports_headless,
        client_name=client_name,
    )
    # Search results first: on a truncated full list, a search hit is the
    # ONLY place a past-the-cutoff name shows up at all.
    all_names = list(dict.fromkeys([*discovery.matches, *discovery.all_names]))
    matches: list[NinjaReportCandidateDTO] = []
    others: list[NinjaReportCandidateDTO] = []
    for name in all_names:
        candidate = NinjaReportCandidateDTO(
            name=name, exact_match=_name_matches_client(name, client_name)
        )
        (matches if candidate.exact_match else others).append(candidate)
    # task-49: `discovery.truncated` alone cannot be trusted to mean "this
    # list is complete" — measured live (2026-08-19) it reported `False` over
    # a list of 16 names that stopped at `GRP-Internal report`, because
    # `_scroll_all_names`' "stopped growing" heuristic reads a STALLED scroll
    # exactly like a finished one. Search is the ground truth here, so a match
    # it found that the scroll list lacks PROVES the list is partial no matter
    # what the flag says. Never present a truncated list as an exact total
    # (PLAN §12 / AGENTS §5) — the operator must not read a name's absence
    # from `others` as "this report does not exist".
    scrolled = set(discovery.all_names)
    provably_partial = any(name not in scrolled for name in discovery.matches)
    return NinjaReportNameDTO(
        client_id=client_id,
        client_name=client_name,
        ninja_report_name=current,
        matches=matches,
        others=others,
        others_truncated=discovery.truncated or provably_partial,
    )


@router.get("/clients/{client_id}/ninja-report", responses={404: {"model": ErrorDTO}})
async def get_ninja_report_name(client_id: str) -> NinjaReportNameDTO:
    dto = await run_in_threadpool(_discover_ninja_report, client_id)
    if dto is None:
        raise HTTPException(404, _CLIENT_NOT_FOUND)
    return dto


def _save_ninja_report_name(client_id: str, name: str, *, confirm_mismatch: bool) -> str:
    """Validate, then save. Returns an outcome code, never raises.

    The UI cannot be the only safeguard against §1.2 (a saved binding to
    another client's report is permanent and silently poisons every later PDF
    run), so both checks live here too:

    1. the name must be one the cabinet actually has — asked via the cabinet's
       own search (`_cabinet_has_report_name`), which no amount of table
       virtualization can hide a row from. No credentials configured means no
       browser, hence nothing to ask; that is reported as "could not verify",
       not as "rejected", so a credential-less environment stays usable and
       never silently downgrades to a weaker check that LOOKS like it
       verified something;
    2. a name that does not start with the client's name requires an explicit
       `confirm_mismatch`.
    """
    identity = _client_identity(client_id)
    if identity is None:
        return "client_not_found"
    client_name, _saved, has_integration = identity
    if not has_integration:
        return "not_configured"

    # `None` = could not ask (no credentials), which is never a rejection
    # (task-48 §1.7). `False` = the cabinet's own search, which the operator's
    # candidate list came from in the first place, does not have this name.
    if _cabinet_has_report_name(name) is False:
        return "report_name_unknown"
    if not _name_matches_client(name, client_name) and not confirm_mismatch:
        return "report_name_mismatch"

    with worker_session_scope() as session:
        client_repo = ClientRepository(session)
        integration = client_repo.get_integration(client_id, Provider.reporting_ninja)
        if integration is None:  # pragma: no cover - re-checked inside the write transaction
            return "not_configured"
        config = dict(integration.config_json)
        config["ninja_report_name"] = name
        client_repo.upsert_integration(
            client_id, Provider.reporting_ninja,
            enabled=integration.enabled, source_mode=integration.source_mode, config=config,
        )
        return "ok"


_SAVE_ERRORS = {
    "not_configured": "Reporting Ninja is not configured for this client",
    "report_name_unknown": (
        "That report name does not exist in the Reporting Ninja account. "
        "Re-run discovery and choose a name from the list."
    ),
    "report_name_mismatch": (
        "This report does not belong to this client by name. "
        "Re-send with confirm_mismatch=true if the binding is deliberate."
    ),
}


@router.put(
    "/clients/{client_id}/ninja-report",
    responses={404: {"model": ErrorDTO}, 422: {"model": ErrorDTO}},
)
async def put_ninja_report_name(
    client_id: str, body: NinjaReportNameUpdateRequest
) -> NinjaReportNameDTO:
    outcome = await run_in_threadpool(
        _save_ninja_report_name, client_id, body.ninja_report_name,
        confirm_mismatch=body.confirm_mismatch,
    )
    if outcome == "client_not_found":
        raise HTTPException(404, _CLIENT_NOT_FOUND)
    if outcome != "ok":
        raise HTTPException(
            422, ErrorDTO(error_code=outcome, message=_SAVE_ERRORS[outcome]).model_dump()
        )
    dto = await run_in_threadpool(_current_ninja_report, client_id)
    if dto is None:  # pragma: no cover - can't disappear between the two calls above
        raise HTTPException(404, _CLIENT_NOT_FOUND)
    return dto


# --- GET /ninja-bindings (task-49 §2.1) -------------------------------------
#
# One DB-only pass over every client, no browser call — the live incident this
# closes was 9/12 clients failing at 0 seconds for a reason (`report_name_missing`)
# that was sitting in `client_integrations` the whole time; the wizard just
# never had a way to read it before starting a batch.


def _binding_row(client_repo: ClientRepository, client: Any) -> NinjaBindingRowDTO:
    integration = client_repo.get_integration(client.id, Provider.reporting_ninja)
    connected = bool(integration and integration.config_json.get("connection_key"))
    ninja_report_name = integration.config_json.get("ninja_report_name") if integration else None

    state: NinjaBindingState
    if not connected:
        state = "not_connected"
    elif not ninja_report_name:
        state = "no_report_name"
    elif not _name_matches_client(ninja_report_name, client.name):
        state = "foreign_binding"
    else:
        state = "ready"

    return NinjaBindingRowDTO(
        client_id=client.id, name=client.name, domain=client.domain,
        connected=connected, ninja_report_name=ninja_report_name, state=state,
    )


def _build_ninja_bindings() -> NinjaBindingsDTO:
    with worker_session_scope() as session:
        client_repo = ClientRepository(session)
        clients = client_repo.list_all()
        rows = [_binding_row(client_repo, client) for client in clients]
    return NinjaBindingsDTO(rows=rows)


@router.get("/ninja-bindings")
async def get_ninja_bindings() -> NinjaBindingsDTO:
    return await run_in_threadpool(_build_ninja_bindings)


# --- GET /runs/{id}/document, POST /runs/{id}/client-pdf (task-47 §2.1/§5) --
#
# One rendered document, two surfaces (task-47 §2.1): `/document` is the
# exact HTML the operator's `<iframe>` shows AND the URL Chromium renders to
# produce the PDF (`app.reports.client_pdf`) — there is no second template
# to drift out of sync with the first.


def _sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _month_stamp_from_result(result: dict[str, Any], fallback: datetime) -> str:
    for period in result.get("periods", []):
        if period.get("label") == "This Month" and period.get("date_from"):
            return str(period["date_from"])[:7]
    return fallback.strftime("%Y-%m")


def _document_source(run_id: str) -> tuple[Any, str] | None:
    with worker_session_scope() as session:
        run = ReportRepository(session).get(run_id)
        if run is None:
            return None
        client = ClientRepository(session).get(run.client_id)
        return run, (client.name if client else run.client_id)


@router.get(
    "/runs/{run_id}/document", responses={404: {"model": ErrorDTO}, 422: {"model": ErrorDTO}}
)
async def get_report_document(run_id: str) -> HTMLResponse:
    found = await run_in_threadpool(_document_source, run_id)
    if found is None:
        raise HTTPException(404, _RUN_NOT_FOUND)
    run, client_name = found
    if run.result_json is None:
        raise HTTPException(422, _RUN_NOT_FINISHED)
    html = render_document_html(
        client_name=client_name, table=run.result_json,
        generated_at=run.finished_at or datetime.now(UTC),
    )
    # `SecurityHeadersMiddleware` sets a blanket `X-Frame-Options: DENY` via
    # `setdefault` (app/core/security.py), which otherwise blocks this exact
    # response from painting inside the run detail page's `<iframe>`
    # (task-47 §2.1 — found live, empty iframe, same class of bug as the
    # inline-PDF viewer's own note in `download_report_file` below). This
    # document is only ever meant to be framed by THIS app (the preview) or
    # driven directly by Chromium (`client_pdf.py`), never a third party.
    return HTMLResponse(
        html,
        headers={
            "X-Frame-Options": "SAMEORIGIN",
            "Content-Security-Policy": (
                "default-src 'self'; img-src 'self' data: https: http:; "
                "frame-ancestors 'self'"
            ),
        },
    )


def _regenerate_client_pdf(run_id: str) -> tuple[str | None, ReportFileDTO | None]:
    settings = get_settings()
    with worker_session_scope() as session:
        run = ReportRepository(session).get(run_id)
        if run is None:
            return "run_not_found", None
        if run.result_json is None:
            return "run_not_finished", None
        client = ClientRepository(session).get(run.client_id)
        client_name = client.name if client else run.client_id
        client_id = run.client_id
        month_stamp = _month_stamp_from_result(run.result_json, run.queued_at)

    artifacts_root = settings.artifacts_dir / "reports" / run_id
    pdf_path = artifacts_root / client_pdf_filename(client_name, month_stamp)
    render_client_pdf(
        settings=settings, run_id=run_id, out_path=pdf_path, headless=settings.reports_headless
    )
    with worker_session_scope() as session:
        file = ReportRepository(session).create_file(
            client_id, run_id=run_id, kind="client_pdf",
            path=str(pdf_path.relative_to(settings.artifacts_dir)),
            sha256=_sha256_of(pdf_path), size_bytes=pdf_path.stat().st_size, mime="application/pdf",
        )
        return None, ReportFileDTO(
            id=file.id, run_id=file.run_id, kind=file.kind,  # type: ignore[arg-type]
            period_label=file.period_label, date_from=file.date_from, date_to=file.date_to,
            is_partial_period=file.is_partial_period, size_bytes=file.size_bytes, mime=file.mime,
            created_at=file.created_at,
        )


@router.post(
    "/runs/{run_id}/client-pdf", responses={404: {"model": ErrorDTO}, 422: {"model": ErrorDTO}}
)
async def create_client_pdf(run_id: str) -> ReportFileDTO:
    error, dto = await run_in_threadpool(_regenerate_client_pdf, run_id)
    if error == "run_not_found":
        raise HTTPException(404, _RUN_NOT_FOUND)
    if error == "run_not_finished":
        raise HTTPException(422, _RUN_NOT_FINISHED)
    if dto is None:  # pragma: no cover - the two error codes above are exhaustive
        raise HTTPException(404, _RUN_NOT_FOUND)
    return dto


# --- Batches (task-47 §2.2/§5) -----------------------------------------------


def _batch_row_dto(run: ReportRun, client_name: str | None) -> ReportBatchClientRowDTO:
    # task-49 §2.6: how many of this client's periods were served from cache
    # — the matrix chip (`6 cached` / `From cache`) reads straight off this,
    # no new collection or repository query needed since `periods_json`
    # already carries each period's `state`.
    cached_count = sum(1 for row in (run.periods_json or []) if row.get("state") == "cached")
    return ReportBatchClientRowDTO(
        run_id=run.id, client_id=run.client_id, client_name=client_name,
        status=run.status,  # type: ignore[arg-type]
        period_count=run.period_count, periods_done=run.periods_done,
        periods_failed=run.periods_failed, error_message=run.error_message,
        error_code=run.error_code, cached_count=cached_count,
        started_at=run.started_at, finished_at=run.finished_at,
    )


def _batch_dto(
    batch: ReportBatch, children: list[ReportRun], client_repo: ClientRepository
) -> ReportBatchDTO:
    # task-49 §2.4: a retried/rebuilt client shows its LATEST run only — the
    # superseded one (same `client_id`, same `batch_id`) simply isn't in this
    # view any more, with no schema change (see `latest_runs_by_client`).
    rows = []
    for child in latest_runs_by_client(children).values():
        client = client_repo.get(child.client_id)
        rows.append(_batch_row_dto(child, client.name if client else None))
    return ReportBatchDTO(
        id=batch.id, kind=batch.kind,  # type: ignore[arg-type]
        source=batch.source,  # type: ignore[arg-type]
        status=batch.status,  # type: ignore[arg-type]
        client_count=batch.client_count, clients_done=batch.clients_done,
        clients_failed=batch.clients_failed, cancel_requested=batch.cancel_requested,
        params=dict(batch.params_json or {}), clients=rows,
        queued_at=batch.queued_at, started_at=batch.started_at, finished_at=batch.finished_at,
    )


def _list_batches(
    *, client_id: str | None, status: str | None, limit: int, offset: int
) -> tuple[list[ReportBatchDTO], int]:
    with worker_session_scope() as session:
        repo = ReportRepository(session)
        client_repo = ClientRepository(session)
        rows, total = repo.list_batches(
            client_id=client_id, status=status, limit=limit, offset=offset
        )
        dtos = [_batch_dto(b, repo.list_runs_for_batch(b.id), client_repo) for b in rows]
        return dtos, total


@router.get("/batches")
async def list_report_batches(
    pagination: PaginationDep, client_id: str | None = None, status: str | None = None
) -> ReportBatchesPageDTO:
    rows, total = await run_in_threadpool(
        _list_batches, client_id=client_id, status=status,
        limit=pagination.limit, offset=pagination.offset,
    )
    return ReportBatchesPageDTO(
        rows=rows, total=total, limit=pagination.limit, offset=pagination.offset
    )


def _get_batch_dto(batch_id: str) -> ReportBatchDTO | None:
    with worker_session_scope() as session:
        repo = ReportRepository(session)
        batch = repo.get_batch(batch_id)
        if batch is None:
            return None
        return _batch_dto(batch, repo.list_runs_for_batch(batch_id), ClientRepository(session))


@router.get("/batches/{batch_id}", responses={404: {"model": ErrorDTO}})
async def get_report_batch(batch_id: str) -> ReportBatchDTO:
    dto = await run_in_threadpool(_get_batch_dto, batch_id)
    if dto is None:
        raise HTTPException(404, _BATCH_NOT_FOUND)
    return dto


def _batch_params_for(body: ReportBatchCreateRequest) -> dict[str, Any]:
    params: dict[str, Any] = {"force_refresh": body.force_refresh}
    if body.kind == "yearly":
        params["years"] = body.years or []
        params["span"] = body.span or "full"
    if body.kind in ("custom_months", "comparator"):
        params["months"] = body.months or []
    return params


def _create_batch(body: ReportBatchCreateRequest) -> tuple[str | None, str | None]:
    """Returns `(error_code, batch_id)` — exactly one is `None`."""
    if not body.client_ids:
        return "no_clients_selected", None
    settings = get_settings()
    params = _batch_params_for(body)
    with worker_session_scope() as session:
        client_repo = ClientRepository(session)
        for client_id in body.client_ids:
            if client_repo.get(client_id) is None:
                return "client_not_found", None
        try:
            batch = create_batch(
                session, client_ids=body.client_ids, kind=body.kind, source=body.source,
                params=params, max_months=settings.reports_max_months,
                max_comparator_years=settings.reports_max_comparator_years,
            )
        except NoClientsSelectedError:
            return "no_clients_selected", None
        except InvalidComparatorMonthsError:
            return "invalid_comparator_months", None
        except (InvalidPeriodError, ValueError):
            return "invalid_period", None
        return None, batch.id


@router.post(
    "/batches", status_code=202, responses={404: {"model": ErrorDTO}, 422: {"model": ErrorDTO}}
)
async def create_report_batch(body: ReportBatchCreateRequest) -> ReportBatchAcceptedDTO:
    error, batch_id = await run_in_threadpool(_create_batch, body)
    if error == "client_not_found":
        raise HTTPException(404, _CLIENT_NOT_FOUND)
    if error == "no_clients_selected":
        raise HTTPException(422, _NO_CLIENTS_SELECTED)
    if error is not None:
        raise HTTPException(
            422, ErrorDTO(error_code=error, message="Invalid report parameters").model_dump()
        )
    if batch_id is None:  # pragma: no cover - exhaustive with the checks above
        raise HTTPException(422, _NO_CLIENTS_SELECTED)
    return ReportBatchAcceptedDTO(
        batch_id=batch_id,
        status_url=f"/api/v1/reports/batches/{batch_id}",
        events_url=f"/api/v1/reports/batches/{batch_id}/events",
    )


@router.post(
    "/batches/{batch_id}/cancel", responses={404: {"model": ErrorDTO}, 409: {"model": ErrorDTO}}
)
async def cancel_report_batch(batch_id: str) -> ReportBatchDTO:
    error = await run_in_threadpool(cancel_batch, worker_session_scope, batch_id)
    if error == "batch_not_found":
        raise HTTPException(404, _BATCH_NOT_FOUND)
    if error == "batch_not_cancellable":
        raise HTTPException(409, _BATCH_NOT_CANCELLABLE)
    dto = await run_in_threadpool(_get_batch_dto, batch_id)
    if dto is None:  # pragma: no cover - can't disappear between the two calls above
        raise HTTPException(404, _BATCH_NOT_FOUND)
    return dto


# --- POST /batches/{id}/clients/{id}/retry (task-49 §2.4) -------------------
#
# Replaces the old "Retry this client" behaviour (`createReportRun` with the
# batch's own params, spread with a silent `force_refresh: false`), which
# created a standalone run OUTSIDE the batch — the batch itself could then
# never reach `succeeded` even once every client was actually done. This one
# stays inside the batch (`rebuild_run` preserves `batch_id`) and requires the
# caller to state `force_refresh` explicitly.


def _retry_batch_client(
    batch_id: str, client_id: str, *, force_refresh: bool
) -> tuple[str | None, str | None]:
    settings = get_settings()
    with worker_session_scope() as session:
        repo = ReportRepository(session)
        if repo.get_batch(batch_id) is None:
            return "batch_not_found", None
        children = repo.list_runs_for_batch(batch_id)
        current = latest_runs_by_client(children).get(client_id)
        if current is None:
            return "client_not_in_batch", None
        source_run_id = current.id
    error, _new_run_id = rebuild_run(
        worker_session_scope, source_run_id, force_refresh=force_refresh,
        max_months=settings.reports_max_months,
        max_comparator_years=settings.reports_max_comparator_years,
    )
    if error == "run_not_retryable":
        return "client_not_retryable", None
    return error, batch_id


_BATCH_CLIENT_RETRY_ERROR_RESPONSES = {
    "batch_not_found": _BATCH_NOT_FOUND,
    "client_not_in_batch": _CLIENT_NOT_IN_BATCH,
    "client_not_retryable": _CLIENT_NOT_RETRYABLE,
}


@router.post(
    "/batches/{batch_id}/clients/{client_id}/retry",
    responses={404: {"model": ErrorDTO}, 409: {"model": ErrorDTO}},
)
async def retry_batch_client(
    batch_id: str, client_id: str, body: BatchClientRetryRequest
) -> ReportBatchDTO:
    error, _batch_id = await run_in_threadpool(
        _retry_batch_client, batch_id, client_id, force_refresh=body.force_refresh
    )
    if error is not None:
        status_code = 404 if error in ("batch_not_found", "client_not_in_batch") else 409
        raise HTTPException(status_code, _BATCH_CLIENT_RETRY_ERROR_RESPONSES[error])
    dto = await run_in_threadpool(_get_batch_dto, batch_id)
    if dto is None:  # pragma: no cover - can't disappear between the two calls above
        raise HTTPException(404, _BATCH_NOT_FOUND)
    return dto


# --- GET /batches/{id}/events (SSE, aggregated across every child run) -------
#
# There is no single monotonic `seq` across runs (`ReportRunEvent.seq` is
# per-run, task-42). A stable synthetic one is assigned by re-sorting every
# child's events by `(created_at, run_id, seq)` on each poll and numbering
# the result 1..N — events are only ever appended, never reordered, so an
# event's position in that sort is stable across polls and safe to use as
# `Last-Event-ID`. Batches are small (a portfolio, not a firehose), so a full
# re-scan every ~2s is cheap.


def _fetch_batch_events(batch_id: str, last_seq: int) -> list[_SseEvent]:
    with worker_session_scope() as session:
        repo = ReportRepository(session)
        if repo.get_batch(batch_id) is None:
            return []
        children = repo.list_runs_for_batch(batch_id)
        client_repo = ClientRepository(session)
        names: dict[str, str] = {}
        for child in children:
            if child.client_id not in names:
                client = client_repo.get(child.client_id)
                names[child.client_id] = client.name if client else child.client_id
        combined = [
            (
                event.created_at, child.id, event.seq, event,
                names.get(child.client_id, child.client_id),
            )
            for child in children
            for event in repo.events_since(child.id, 0)
        ]
        combined.sort(key=lambda row: (row[0], row[1], row[2]))
        return [
            _SseEvent(
                idx, event.level, event.stage, f"[{name}] {event.message}",
                event.details_json, event.created_at,
            )
            for idx, (_created_at, _run_id, _seq, event, name) in enumerate(combined, start=1)
            if idx > last_seq
        ]


def _batch_is_done(batch_id: str) -> bool:
    with worker_session_scope() as session:
        batch = ReportRepository(session).get_batch(batch_id)
        return batch is None or batch.status in _TERMINAL_STATUSES


@router.get("/batches/{batch_id}/events")
async def report_batch_events(batch_id: str, request: Request) -> StreamingResponse:
    header = request.headers.get("last-event-id") or request.query_params.get("last_event_id")
    last_event_id = int(header) if header else 0

    async def fetch_since(last_seq: int) -> list[Any]:
        return await run_in_threadpool(_fetch_batch_events, batch_id, last_seq)

    async def is_done() -> bool:
        return await run_in_threadpool(_batch_is_done, batch_id)

    return StreamingResponse(
        event_stream(fetch_since, last_event_id=last_event_id, is_done=is_done),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# --- GET /batches/{id}/pdf-archive -------------------------------------------
#
# Built fresh on every request rather than cached as a `report_files` row
# (task-47 §3.3 describes caching it there): a batch-level file has no single
# owning client, and `report_files.client_id` is `NOT NULL` (task-42) — adding
# a second, batch-owned identity to that table for one convenience cache was
# not worth the schema change. Zipping a portfolio's worth of ~50-150 KB PDFs
# is sub-second, so there is nothing to cache in practice (documented as a
# deliberate simplification in this task's Completion report).


def _build_batch_archive(batch_id: str) -> tuple[str, bytes] | None:
    settings = get_settings()
    with worker_session_scope() as session:
        repo = ReportRepository(session)
        if repo.get_batch(batch_id) is None:
            return None
        children = repo.list_runs_for_batch(batch_id)
        pdf_files: list[tuple[str, Path]] = []
        month_stamp: str | None = None
        for child in children:
            if month_stamp is None and child.result_json:
                for period in child.result_json.get("periods", []):
                    if period.get("label") == "This Month" and period.get("date_from"):
                        month_stamp = str(period["date_from"])[:7]
                        break
            for file in repo.list_files_for_run(child.id):
                if file.kind == "client_pdf":
                    pdf_files.append((Path(file.path).name, settings.artifacts_dir / file.path))

    buffer = io.BytesIO()
    written = 0
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, path in pdf_files:
            if path.is_file():
                archive.write(path, arcname=name)
                written += 1
    # An archive with nothing in it is a failure wearing a success costume:
    # the browser saves a valid, empty .zip and the operator has no idea why
    # (live 2026-08-19 — a 9-client batch downloaded 0 bytes of report
    # because no `client_pdf` had ever been rendered, see the ordering fix in
    # `app/reports/runner.py`). Say so instead of shipping the emptiness.
    if written == 0:
        return None
    filename = f"TheYovth_Monthly_{month_stamp or date.today().strftime('%Y-%m')}.zip"
    return filename, buffer.getvalue()


def _batch_exists(batch_id: str) -> bool:
    with worker_session_scope() as session:
        return ReportRepository(session).get_batch(batch_id) is not None


_NO_CLIENT_PDFS = ErrorDTO(
    error_code="no_client_pdfs",
    message=(
        "No client PDFs have been generated for this batch yet. "
        "Open a client's report and use Generate PDF, or rebuild the batch."
    ),
).model_dump()


@router.get("/batches/{batch_id}/pdf-archive", responses={404: {"model": ErrorDTO}})
async def download_batch_pdf_archive(batch_id: str) -> StreamingResponse:
    exists = await run_in_threadpool(_batch_exists, batch_id)
    if not exists:
        raise HTTPException(404, _BATCH_NOT_FOUND)
    result = await run_in_threadpool(_build_batch_archive, batch_id)
    if result is None:
        raise HTTPException(404, _NO_CLIENT_PDFS)
    filename, data = result
    return StreamingResponse(
        iter([data]), media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# --- GET /batches/{id}/portfolio.pdf, /batches/{id}/portfolio.xlsx (task-50) -
#
# One combined document across the whole batch, instead of the ZIP's N
# separate files above — same "built fresh on every request, nothing to
# cache" reasoning (§pdf-archive comment): concatenating already-rendered
# PDFs with `pdfunite` and writing an XLSX with `openpyxl` are both
# sub-second, no browser involved. Uses `latest_runs_by_client` (not
# `list_runs_for_batch` directly, unlike the ZIP above) so a client retried
# mid-batch contributes only its newest result — never a stale page/tab
# alongside the fresh one.


def _batch_month_stamp(children: list[ReportRun]) -> str | None:
    for child in children:
        if not child.result_json:
            continue
        for period in child.result_json.get("periods", []):
            if period.get("label") == "This Month" and period.get("date_from"):
                return str(period["date_from"])[:7]
    return None


def _latest_children_by_name(
    batch_id: str,
) -> tuple[list[tuple[str, ReportRun]], str | None] | None:
    """`[(client_name, run), ...]` sorted by name, and the batch's own month
    stamp — or `None` if the batch itself does not exist.
    """
    with worker_session_scope() as session:
        repo = ReportRepository(session)
        if repo.get_batch(batch_id) is None:
            return None
        client_repo = ClientRepository(session)
        children = list(latest_runs_by_client(repo.list_runs_for_batch(batch_id)).values())
        month_stamp = _batch_month_stamp(children)
        named = []
        for child in children:
            client = client_repo.get(child.client_id)
            named.append((client.name if client else child.client_id, child))
    named.sort(key=lambda item: item[0].upper())
    return named, month_stamp


def _build_portfolio_pdf(batch_id: str) -> tuple[str, bytes] | None:
    found = _latest_children_by_name(batch_id)
    if found is None:
        return None
    named, month_stamp = found

    settings = get_settings()
    paths: list[Path] = []
    with worker_session_scope() as session:
        repo = ReportRepository(session)
        for _name, child in named:
            for file in repo.list_files_for_run(child.id):
                if file.kind == "client_pdf":
                    path = settings.artifacts_dir / file.path
                    if path.is_file():
                        paths.append(path)
                    break

    try:
        with tempfile.TemporaryDirectory() as tmp:
            out_path = build_portfolio_pdf(paths, Path(tmp) / "portfolio.pdf")
            data = out_path.read_bytes()
    except NoClientPdfsError:
        return None
    filename = f"TheYovth_Portfolio_{month_stamp or date.today().strftime('%Y-%m')}.pdf"
    return filename, data


def _build_portfolio_xlsx(batch_id: str) -> tuple[str, bytes] | None:
    found = _latest_children_by_name(batch_id)
    if found is None:
        return None
    named, month_stamp = found

    rows: list[tuple[str, ReportTable]] = []
    for name, child in named:
        if not child.result_json:
            continue
        rows.append((name, ReportTable.from_dict(child.result_json, kind=child.kind)))

    try:
        with tempfile.TemporaryDirectory() as tmp:
            out_path = build_portfolio_xlsx(rows, Path(tmp) / "portfolio.xlsx")
            data = out_path.read_bytes()
    except NoClientDataError:
        return None
    filename = f"TheYovth_Portfolio_{month_stamp or date.today().strftime('%Y-%m')}.xlsx"
    return filename, data


@router.get("/batches/{batch_id}/portfolio.pdf", responses={404: {"model": ErrorDTO}})
async def download_batch_portfolio_pdf(
    batch_id: str,
    disposition: str = Query("attachment", pattern="^(inline|attachment)$"),
) -> StreamingResponse:
    exists = await run_in_threadpool(_batch_exists, batch_id)
    if not exists:
        raise HTTPException(404, _BATCH_NOT_FOUND)
    result = await run_in_threadpool(_build_portfolio_pdf, batch_id)
    if result is None:
        raise HTTPException(404, _NO_CLIENT_PDFS)
    filename, data = result
    headers = {"Content-Disposition": f'{disposition}; filename="{filename}"'}
    if disposition == "inline":
        # Same override `GET /files/{id}` uses (see its own comment) — the
        # blanket `X-Frame-Options: DENY`/default CSP otherwise makes the
        # in-page preview iframe render nothing.
        headers["X-Frame-Options"] = "SAMEORIGIN"
        headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data: https: http:; frame-ancestors 'self'"
        )
    return StreamingResponse(iter([data]), media_type="application/pdf", headers=headers)


_NO_CLIENT_RESULTS = ErrorDTO(
    error_code="no_client_results",
    message="No client in this batch has a finished result to export yet.",
).model_dump()


@router.get("/batches/{batch_id}/portfolio.xlsx", responses={404: {"model": ErrorDTO}})
async def download_batch_portfolio_xlsx(batch_id: str) -> StreamingResponse:
    exists = await run_in_threadpool(_batch_exists, batch_id)
    if not exists:
        raise HTTPException(404, _BATCH_NOT_FOUND)
    result = await run_in_threadpool(_build_portfolio_xlsx, batch_id)
    if result is None:
        raise HTTPException(404, _NO_CLIENT_RESULTS)
    filename, data = result
    return StreamingResponse(
        iter([data]),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# --- GET /batches/{id}/portfolio-table (task-50) -----------------------------
#
# On-page table preview, no download: the JSON `ReportTableDTO` `GET
# /runs/{id}/table` already returns per run, one per client — an XLSX has no
# browser-native inline viewer the way a PDF does (the portfolio PDF above
# gets `?disposition=inline`), so the table is the actual, renderable
# on-page substitute for opening the portfolio XLSX.


def _batch_portfolio_table(batch_id: str) -> list[BatchPortfolioTableRowDTO] | None:
    found = _latest_children_by_name(batch_id)
    if found is None:
        return None
    named, _month_stamp = found
    rows: list[BatchPortfolioTableRowDTO] = []
    for name, child in named:
        dto = _get_table(child.id)
        if dto is not None:
            rows.append(
                BatchPortfolioTableRowDTO(client_id=child.client_id, client_name=name, table=dto)
            )
    return rows


@router.get("/batches/{batch_id}/portfolio-table", responses={404: {"model": ErrorDTO}})
async def get_batch_portfolio_table(batch_id: str) -> BatchPortfolioTableDTO:
    rows = await run_in_threadpool(_batch_portfolio_table, batch_id)
    if rows is None:
        raise HTTPException(404, _BATCH_NOT_FOUND)
    return BatchPortfolioTableDTO(clients=rows)


# --- GET /accuracy (task-48 §2.4) ----------------------------------------
#
# `GET /reports/accuracy` machine-builds the same table task-48 §1 had to
# assemble by hand from the database and a live Reporting Ninja check —
# bindings, cross-check verdicts and GSC coverage for every client, one pass.
# `list_client_accuracy` is also called directly by `yovth-report
# accuracy-report` (app/cli.py) — no HTTP round trip for the CLI.


def _accuracy_row(session: Session, client: Any) -> AccuracyClientRowDTO:
    client_repo = ClientRepository(session)
    rn_integration = client_repo.get_integration(client.id, Provider.reporting_ninja)
    gsc_integration = client_repo.get_integration(client.id, Provider.gsc)
    has_gsc = bool(gsc_integration and gsc_integration.config_json.get("connection_key"))

    if rn_integration is None or not rn_integration.config_json.get("connection_key"):
        return AccuracyClientRowDTO(
            client_id=client.id, client_name=client.name,
            binding_status="not_configured", has_gsc=has_gsc,
        )
    ninja_report_name = rn_integration.config_json.get("ninja_report_name")
    binding_status: BindingStatus
    if not ninja_report_name:
        binding_status = "missing"
    elif _name_matches_client(ninja_report_name, client.name):
        binding_status = "ok"
    else:
        binding_status = "foreign"

    rows, _total = ReportRepository(session).list_runs(
        client_id=client.id, source="pdf", limit=20, offset=0
    )
    latest_with_cc = next(
        (r for r in rows if r.cross_check_status is not None and r.result_json), None
    )
    match_count = no_data_count = differ_count = 0
    cross_check_status: CrossCheckStatus | None = None
    latest_pdf_run_id = None
    checked_at = None
    if latest_with_cc is not None and latest_with_cc.result_json is not None:
        cells = (latest_with_cc.result_json.get("cross_check") or {}).get("cells", [])
        no_data_count = sum(1 for c in cells if c["verdict"] == "no_data")
        differ_count = sum(1 for c in cells if c["verdict"] in DIFFER_VERDICTS)
        match_count = len(cells) - no_data_count - differ_count
        cross_check_status = latest_with_cc.cross_check_status  # type: ignore[assignment]
        latest_pdf_run_id = latest_with_cc.id
        checked_at = latest_with_cc.finished_at

    return AccuracyClientRowDTO(
        client_id=client.id, client_name=client.name, ninja_report_name=ninja_report_name,
        binding_status=binding_status, has_gsc=has_gsc, latest_pdf_run_id=latest_pdf_run_id,
        cross_check_status=cross_check_status, match_count=match_count,
        no_data_count=no_data_count, differ_count=differ_count, checked_at=checked_at,
    )


def list_client_accuracy(session: Session) -> list[AccuracyClientRowDTO]:
    clients = ClientRepository(session).list_all()
    return [_accuracy_row(session, client) for client in clients]


def _build_accuracy_report() -> AccuracyReportDTO:
    with worker_session_scope() as session:
        clients = list_client_accuracy(session)
    return AccuracyReportDTO(generated_at=datetime.now(UTC), clients=clients)


@router.get("/accuracy")
async def get_accuracy_report() -> AccuracyReportDTO:
    return await run_in_threadpool(_build_accuracy_report)


# --- POST /runs/{id}/periods/{label}/retry (task-47 follow-up) --------------
#
# Manual, single-shot retry of exactly ONE failed period on an already-
# finished run — the automatic 3-attempt budget (`ninja_browser`'s own retry
# loop / `runner._fetch_period_metrics_with_retry`) is exhausted by the time
# an operator reaches for this. Re-running the whole report would discard
# every period that already succeeded.

_PERIOD_RETRY_ERROR_RESPONSES = {
    "run_not_found": _RUN_NOT_FOUND,
    "run_not_finished": _RUN_NOT_FINISHED,
    "period_not_found": _PERIOD_NOT_FOUND,
    "period_not_retryable": _PERIOD_NOT_RETRYABLE,
}


@router.post(
    "/runs/{run_id}/periods/{label}/retry",
    responses={404: {"model": ErrorDTO}, 409: {"model": ErrorDTO}},
)
async def retry_report_period(run_id: str, label: str) -> ReportRunDTO:
    settings = get_settings()
    error = await run_in_threadpool(
        retry_period, worker_session_scope, settings, run_id, label
    )
    if error is not None:
        status_code = 404 if error in ("run_not_found", "period_not_found") else 409
        raise HTTPException(status_code, _PERIOD_RETRY_ERROR_RESPONSES[error])
    dto = await run_in_threadpool(_get_run, run_id)
    if dto is None:  # pragma: no cover - can't disappear between the two calls above
        raise HTTPException(404, _RUN_NOT_FOUND)
    return dto


register_router(router)
