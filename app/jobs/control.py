"""Cancel / retry-failed-only control operations (PLAN §9.2/§9.3).

Thin orchestration over the repository primitives: `POST /sync-jobs/{id}/cancel`
and `.../retry` (task-06) call these instead of juggling `CellRepository` and
`JobRepository` calls themselves. Cancellation is cooperative — this only
flags rows; a lane's `runner.run_cell` observes the flag at its next poll and
brings the in-flight cell to a stop (PLAN §9.3 step 3/`is_cancel_requested`).
"""

from __future__ import annotations

from typing import Any

from app.db.models import SyncJobProvider
from app.db.repositories.cells import CellRepository
from app.db.repositories.jobs import JobRepository
from app.jobs.events import append_cell_status_event
from app.schemas.enums import CellStatus, JobStatus


def cancel_run(
    session_scope: Any, *, job_id: str | None = None, batch_id: str | None = None
) -> JobStatus:
    """Flag every non-terminal cell (and the job/batch row itself)
    `cancel_requested`, then immediately finalize the ones no runner can ever
    observe. Idempotent: cells already terminal are left alone.

    Cancellation is cooperative only for a cell that is actually `running` —
    `runner.run_cell` polls the flag mid-`collect()` and stops. Nothing polls
    it on behalf of a cell that was merely `queued` (never claimed — and once
    flagged it can no longer be claimed, since `claim_next_cell` only takes
    `queued`) or parked on `awaiting_auth`. Those used to sit at
    `cancel_requested` forever, which is neither terminal nor running, so the
    job never reached a terminal `aggregate_status` and its SSE stream never
    closed. Finalizing them here is safe precisely because no runner owns them.
    """
    if job_id is None and batch_id is None:
        raise ValueError("cancel_run requires job_id or batch_id")
    with session_scope() as session:
        cells = CellRepository(session)
        jobs = JobRepository(session)

        def scoped_cells() -> list[SyncJobProvider]:
            if job_id is not None:
                return cells.list_for_job(job_id)
            assert batch_id is not None
            return cells.list_for_batch(batch_id)

        scoped = scoped_cells()
        # Captured *before* `request_cancel` rewrites the status, since it
        # collapses both `queued` and `running` into `cancel_requested`.
        running_ids = {cell.id for cell in scoped if cell.status == CellStatus.running}

        cells.request_cancel(job_id=job_id, batch_id=batch_id)
        jobs.request_cancel(job_id=job_id, batch_id=batch_id)

        # Re-read: `request_cancel` is a bulk UPDATE, so `scoped`'s in-memory
        # statuses are not a reliable view of what it just wrote.
        refreshed = scoped_cells()
        affected_job_ids: set[str] = set()
        for cell in refreshed:
            if cell.id in running_ids or cell.status != CellStatus.cancel_requested:
                continue
            cells.set_status(cell.id, CellStatus.cancelled, error_code=None, error_message=None)
            append_cell_status_event(
                session,
                job_id=cell.job_id,
                provider=cell.provider,
                status=CellStatus.cancelled,
            )
            affected_job_ids.add(cell.job_id)

        for jid in affected_job_ids:
            jobs.sync_status_from_cells(jid)

        return jobs.aggregate_status(job_id=job_id, batch_id=batch_id)


def retry_failed_only(
    session_scope: Any, *, job_id: str | None = None, batch_id: str | None = None
) -> int:
    """Requeue only `failed`/`failed_interrupted` cells for a fresh attempt —
    successful cells are never touched (PLAN §9.2 "retry failed only").

    Returns the number of cells requeued. The requeued cells simply become
    `queued` again with `attempt += 1`; the existing lanes pick them up on
    their next poll, no separate "retry job" bookkeeping needed.
    """
    if job_id is None and batch_id is None:
        raise ValueError("retry_failed_only requires job_id or batch_id")
    with session_scope() as session:
        cells = CellRepository(session)
        jobs = JobRepository(session)
        failed = cells.failed_cells(job_id=job_id, batch_id=batch_id)
        job_ids: set[str] = set()
        for cell in failed:
            cells.requeue_for_retry(cell.id)
            job_ids.add(cell.job_id)
        for jid in job_ids:
            jobs.sync_status_from_cells(jid)
        return len(failed)
