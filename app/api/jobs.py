"""Sync-job endpoints (PLAN §9/§10): create/inspect/cancel/retry a collection
run and stream its progress over SSE.

Unlike `dashboard.py`/`clients.py`, this router talks to the database the
same way the job engine (task-05) does — a short-lived sync `Session` from
`app.db.session.worker_session_scope`, bridged onto the event loop with
`run_in_threadpool` — rather than the request-scoped async session. Two
reasons: `app.jobs.control.cancel_run`/`retry_failed_only` already own their
session lifecycle end-to-end and were written against that scope, and using
one fresh sync session per read avoids ever observing a stale SQLite
snapshot from a transaction opened before another connection's commit.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy.orm import Session

from app.api import register_router
from app.core.config import get_settings
from app.core.security import PathTraversalError
from app.db.models import JobEvent, ProviderSnapshot, SyncJob, SyncJobProvider, SyncJobStep
from app.db.repositories.artifacts import ArtifactRepository
from app.db.repositories.cells import CellRepository
from app.db.repositories.clients import ClientRepository
from app.db.repositories.events import EventRepository
from app.db.repositories.jobs import JobRepository
from app.db.repositories.snapshots import SnapshotRepository
from app.db.repositories.steps import StepRepository
from app.db.session import worker_session_scope
from app.jobs.control import cancel_run, retry_failed_only
from app.jobs.events import event_stream
from app.schemas.api import (
    BatchProgressDTO,
    CellProgressDTO,
    ErrorDTO,
    JobDTO,
    JobStepDTO,
    SyncJobAcceptedDTO,
    SyncJobRequest,
)
from app.schemas.enums import METRIC_PROVIDERS, JobStatus, Provider, Quality

router = APIRouter(prefix="/api/v1", tags=["jobs"])

_JOB_TERMINAL = {JobStatus.succeeded, JobStatus.partial, JobStatus.failed, JobStatus.cancelled}
_JOB_NOT_FOUND = ErrorDTO(
    error_code="job_not_found", message="Job or batch not found"
).model_dump()
_ARTIFACT_NOT_FOUND = ErrorDTO(
    error_code="artifact_not_found", message="Artifact not found"
).model_dump()


class ClientsNotFoundError(Exception):
    def __init__(self, client_ids: list[str]) -> None:
        super().__init__(f"unknown client_ids: {client_ids}")
        self.client_ids = client_ids


def _to_cell_dto(cell: SyncJobProvider, quality: Quality | None = None) -> CellProgressDTO:
    return CellProgressDTO(
        client_id=cell.client_id,
        provider=cell.provider,
        status=cell.status,
        quality=quality,
        progress_percent=cell.progress_percent,
        current_stage=cell.current_stage,
        progress_message=cell.progress_message,
        attempt=cell.attempt,
        slot=cell.slot,
        error_code=cell.error_code,
        units_spent=cell.units_spent,
        units_estimated=cell.units_estimated,
        started_at=cell.started_at,
        finished_at=cell.finished_at,
    )


def _snapshot_quality(
    snapshots: dict[tuple[str, Provider], ProviderSnapshot], cell: SyncJobProvider
) -> Quality | None:
    snapshot = snapshots.get((cell.job_id, cell.provider))
    return snapshot.quality if snapshot is not None else None


def _to_step_dto(step: SyncJobStep) -> JobStepDTO:
    return JobStepDTO(
        provider=step.provider,
        stage=step.stage,
        status=step.status,
        progress_percent=step.progress_percent,
        attempt=step.attempt,
        started_at=step.started_at,
        finished_at=step.finished_at,
        warning=step.warning_summary,
        error_code=step.error_summary,
        units_spent=step.units_spent,
        units_estimated=step.units_estimated,
    )


def _to_job_dto(session: Session, job: SyncJob) -> JobDTO:
    cells = CellRepository(session).list_for_job(job.id)
    steps = StepRepository(session).list_for_job(job.id)
    snapshots = SnapshotRepository(session).latest_for_jobs([job.id])
    return JobDTO(
        id=job.id,
        batch_id=job.batch_id,
        client_id=job.client_id,
        period_kind=job.period_kind,
        comparison_mode=job.comparison_mode,
        anchor_date=job.anchor_date,
        custom_start=job.custom_start,
        custom_end=job.custom_end,
        requested_providers=[Provider(p) for p in job.requested_providers_json],
        status=job.status,
        progress_percent=job.progress_percent,
        current_stage=job.current_stage,
        attempt=job.attempt,
        created_at=job.created_at,
        started_at=job.started_at,
        finished_at=job.finished_at,
        error_code=job.error_code,
        error_message=job.error_message,
        steps=[_to_step_dto(s) for s in steps],
        cells=[_to_cell_dto(c, _snapshot_quality(snapshots, c)) for c in cells],
    )


def _to_batch_dto(
    session: Session, batch_id: str, cells: list[SyncJobProvider]
) -> BatchProgressDTO:
    status = JobRepository(session).aggregate_status(batch_id=batch_id)
    snapshots = SnapshotRepository(session).latest_for_jobs(
        list({cell.job_id for cell in cells})
    )
    terminal = {
        "succeeded",
        "partial",
        "failed",
        "failed_interrupted",
        "cancelled",
        "not_configured",
        "skipped",
    }
    # A batch is not only "N cells finished": the active cell already carries
    # measured provider progress.  Averaging those real values gives the popup a
    # smooth truthful crawl indicator while terminal cells remain exactly 100%.
    progress_percent = (
        round(
            sum(
                100.0 if c.status.value in terminal else float(c.progress_percent or 0)
                for c in cells
            )
            / len(cells),
            2,
        )
        if cells
        else None
    )
    return BatchProgressDTO(
        batch_id=batch_id,
        status=status,
        progress_percent=progress_percent,
        cells=[_to_cell_dto(c, _snapshot_quality(snapshots, c)) for c in cells],
    )


def _resolve(id_: str) -> tuple[str, JobDTO | BatchProgressDTO] | None:
    """Look `id_` up as a job first, then as a batch. Fresh session per call."""
    with worker_session_scope() as session:
        job = JobRepository(session).get(id_)
        if job is not None:
            return "job", _to_job_dto(session, job)
        cells = CellRepository(session).list_for_batch(id_)
        if cells:
            return "batch", _to_batch_dto(session, id_, cells)
        return None


def _exists_as(id_: str) -> tuple[str, str] | None:
    with worker_session_scope() as session:
        if JobRepository(session).get(id_) is not None:
            return "job", id_
        if CellRepository(session).list_for_batch(id_):
            return "batch", id_
        return None


def _create_jobs(body: SyncJobRequest) -> tuple[list[SyncJob], list[str]]:
    """Створити джоби; повертає ще й тих, кого «all» відкинуло через паузу."""
    with worker_session_scope() as session:
        clients = ClientRepository(session)
        skipped_paused: list[str] = []
        if body.client_ids == "all":
            # «Всі» = ті, кого справді збираємо: паузований клієнт (task-34)
            # сюди не потрапляє. Явно названий — потрапляє (гілка нижче), бо
            # свідомий вибір оператора важливіший за наш запобіжник.
            collectable = clients.list_collectable()
            client_ids = [c.id for c in collectable]
            collectable_ids = set(client_ids)
            skipped_paused = [c.id for c in clients.list_active() if c.id not in collectable_ids]
        else:
            client_ids = list(body.client_ids)
            missing = [cid for cid in client_ids if clients.get(cid) is None]
            if missing:
                raise ClientsNotFoundError(missing)

        # `"all"` means the metric providers a routine sync collects — NOT every
        # `Provider` member. `tech_audit` (task-25) is an on-demand site crawl
        # with its own endpoint; expanding `tuple(Provider)` here would silently
        # start a full crawl for every client on every "sync all".
        providers = list(METRIC_PROVIDERS) if body.providers == "all" else list(body.providers)
        anchor_date = body.anchor or datetime.now(UTC).date()

        jobs = JobRepository(session).create_run(
            client_ids=client_ids,
            providers=providers,
            period_kind=body.period_kind,
            comparison_mode=body.comparison_mode,
            anchor_date=anchor_date,
            custom_start=body.custom_start,
            custom_end=body.custom_end,
            mode=body.mode,
            idempotency_key=body.idempotency_key,
        )
        return jobs, skipped_paused


@router.post("/sync-jobs", status_code=202, response_model=SyncJobAcceptedDTO)
async def create_sync_job(body: SyncJobRequest) -> SyncJobAcceptedDTO:
    try:
        jobs, skipped_paused = await run_in_threadpool(_create_jobs, body)
    except ClientsNotFoundError as exc:
        raise HTTPException(
            status_code=404,
            detail=ErrorDTO(
                error_code="client_not_found",
                message="one or more client_ids do not exist",
                details={"client_ids": exc.client_ids},
            ).model_dump(),
        ) from exc

    if not jobs:
        # Окремий код, коли список порожній саме через паузу (task-34):
        # «no clients matched» тут звучало б як «клієнтів немає взагалі».
        if skipped_paused:
            raise HTTPException(
                status_code=400,
                detail=ErrorDTO(
                    error_code="all_clients_paused",
                    message="every client is paused; resume one or select it explicitly",
                    details={"client_ids": skipped_paused},
                ).model_dump(),
            )
        raise HTTPException(
            status_code=400,
            detail=ErrorDTO(
                error_code="no_clients", message="no clients matched this request"
            ).model_dump(),
        )

    batch_id = jobs[0].batch_id
    id_for_urls = batch_id or jobs[0].id
    status_url = f"/api/v1/sync-jobs/{id_for_urls}"
    return SyncJobAcceptedDTO(
        job_id=None if batch_id else jobs[0].id,
        batch_id=batch_id,
        status_url=status_url,
        events_url=f"{status_url}/events",
        skipped_paused_client_ids=skipped_paused,
    )


@router.get(
    "/sync-jobs/{id}",
    response_model=JobDTO | BatchProgressDTO,
    responses={404: {"model": ErrorDTO}},
)
async def get_sync_job(id: str) -> JobDTO | BatchProgressDTO:
    result = await run_in_threadpool(_resolve, id)
    if result is None:
        raise HTTPException(status_code=404, detail=_JOB_NOT_FOUND)
    return result[1]


@router.get("/sync-jobs", response_model=list[JobDTO])
async def list_sync_jobs(
    status: JobStatus | None = None, client_id: str | None = None, batch_id: str | None = None
) -> list[JobDTO]:
    def _run() -> list[JobDTO]:
        with worker_session_scope() as session:
            repo = JobRepository(session)
            if batch_id is not None:
                jobs = [
                    j
                    for j in repo.list_for_batch(batch_id)
                    if status is None or j.status == status
                ]
            else:
                jobs = repo.list(status=status, client_id=client_id)
            return [_to_job_dto(session, j) for j in jobs]

    return await run_in_threadpool(_run)


@router.post(
    "/sync-jobs/{id}/cancel",
    response_model=JobDTO | BatchProgressDTO,
    responses={404: {"model": ErrorDTO}},
)
async def cancel_sync_job(id: str) -> JobDTO | BatchProgressDTO:
    found = await run_in_threadpool(_exists_as, id)
    if found is None:
        raise HTTPException(status_code=404, detail=_JOB_NOT_FOUND)
    kind, resolved = found
    await run_in_threadpool(
        cancel_run,
        worker_session_scope,
        job_id=resolved if kind == "job" else None,
        batch_id=resolved if kind == "batch" else None,
    )
    result = await run_in_threadpool(_resolve, id)
    assert result is not None
    return result[1]


@router.post(
    "/sync-jobs/{id}/retry",
    response_model=JobDTO | BatchProgressDTO,
    responses={404: {"model": ErrorDTO}},
)
async def retry_sync_job(id: str) -> JobDTO | BatchProgressDTO:
    found = await run_in_threadpool(_exists_as, id)
    if found is None:
        raise HTTPException(status_code=404, detail=_JOB_NOT_FOUND)
    kind, resolved = found
    await run_in_threadpool(
        retry_failed_only,
        worker_session_scope,
        job_id=resolved if kind == "job" else None,
        batch_id=resolved if kind == "batch" else None,
    )
    result = await run_in_threadpool(_resolve, id)
    assert result is not None
    return result[1]


def _job_ids_for(kind: str, resolved: str, session: Session) -> list[str]:
    if kind == "job":
        return [resolved]
    return [j.id for j in JobRepository(session).list_for_batch(resolved)]


def _fetch_events(kind: str, resolved: str, last_seq: int) -> list[JobEvent]:
    # task-75 §3.3: only events older than this window are safe to hand to
    # the client — see `Settings.job_event_cursor_safety_window_ms`'s own
    # docstring for why a plain `seq > last_seq` cursor silently drops
    # events under more than one writer.
    window_ms = get_settings().job_event_cursor_safety_window_ms
    safe_before = datetime.now(UTC) - timedelta(milliseconds=window_ms)
    with worker_session_scope() as session:
        job_ids = _job_ids_for(kind, resolved, session)
        repo = EventRepository(session)
        events: list[JobEvent] = []
        for job_id in job_ids:
            events.extend(repo.since(job_id, last_seq, safe_before=safe_before))
        events.sort(key=lambda e: e.seq)
        return events


def _has_pending_events(kind: str, resolved: str, last_seq: int) -> bool:
    """Is there anything past `last_seq` at all, safety window aside?

    Task-75 §3.3: without this, `stream_sync_job_events` could see an
    empty `_fetch_events` (nothing has aged past the safety window YET)
    at the exact moment the job also happens to look terminal, and close
    the SSE stream before its own last event(s) ever got a chance to age
    past the window and be delivered.
    """
    with worker_session_scope() as session:
        repo = EventRepository(session)
        return any(
            repo.has_pending(job_id, last_seq)
            for job_id in _job_ids_for(kind, resolved, session)
        )


def _job_is_done(kind: str, resolved: str) -> bool:
    with worker_session_scope() as session:
        repo = JobRepository(session)
        if kind == "job":
            job = repo.get(resolved)
            return job is not None and job.status in _JOB_TERMINAL
        return repo.aggregate_status(batch_id=resolved) in _JOB_TERMINAL


@router.get("/sync-jobs/{id}/events", responses={404: {"model": ErrorDTO}})
async def stream_sync_job_events(id: str, request: Request) -> StreamingResponse:
    found = await run_in_threadpool(_exists_as, id)
    if found is None:
        raise HTTPException(status_code=404, detail=_JOB_NOT_FOUND)
    kind, resolved = found

    last_event_id_header = request.headers.get("last-event-id") or request.query_params.get(
        "last_event_id"
    )
    try:
        last_event_id = int(last_event_id_header) if last_event_id_header else 0
    except ValueError:
        last_event_id = 0

    # task-75 §3.3: `is_done` needs to know the cursor `fetch_since` was
    # last called with, to check whether anything is still waiting to age
    # past the safety window before agreeing the stream is finished. The
    # two closures share this one variable rather than widening
    # `event_stream`'s own signature, which `app/api/reports.py` also uses
    # for a wholly separate events table (out of this task's owned paths).
    last_seen_seq = last_event_id

    async def fetch_since(last_seq: int) -> list[JobEvent]:
        nonlocal last_seen_seq
        last_seen_seq = last_seq
        return await run_in_threadpool(_fetch_events, kind, resolved, last_seq)

    async def is_done() -> bool:
        if await run_in_threadpool(_has_pending_events, kind, resolved, last_seen_seq):
            return False
        return await run_in_threadpool(_job_is_done, kind, resolved)

    return StreamingResponse(
        event_stream(fetch_since, last_event_id=last_event_id, is_done=is_done),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


def _fetch_artifact(artifact_id: str) -> tuple[str | None, Path] | None:
    settings = get_settings()
    with worker_session_scope() as session:
        found = ArtifactRepository(session, settings.artifacts_dir).get_for_download(artifact_id)
        if found is None:
            return None
        artifact, resolved = found
        return artifact.mime, resolved


@router.get("/artifacts/{artifact_id}", responses={404: {"model": ErrorDTO}})
async def download_artifact(artifact_id: str) -> FileResponse:
    """Raw diagnostic artifact download (PLAN §13): only ever serves a path
    `ArtifactRepository.get_for_download` has already verified resolves
    inside the artifacts root — never a caller-controlled path.
    """
    try:
        found = await run_in_threadpool(_fetch_artifact, artifact_id)
    except PathTraversalError:
        found = None
    if found is None:
        raise HTTPException(status_code=404, detail=_ARTIFACT_NOT_FOUND)
    mime, resolved = found
    if not resolved.is_file():
        raise HTTPException(status_code=404, detail=_ARTIFACT_NOT_FOUND)
    return FileResponse(resolved, media_type=mime or "application/octet-stream")


register_router(router)
