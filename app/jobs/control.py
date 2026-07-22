"""Cancel / retry-failed-only control operations (PLAN §9.2/§9.3).

Thin orchestration over the repository primitives: `POST /sync-jobs/{id}/cancel`
and `.../retry` (task-06) call these instead of juggling `CellRepository` and
`JobRepository` calls themselves. Cancellation is cooperative — this only
flags rows; a lane's `runner.run_cell` observes the flag at its next poll and
brings the in-flight cell to a stop (PLAN §9.3 step 3/`is_cancel_requested`).
"""

from __future__ import annotations

from typing import Any

from app.db.repositories.cells import CellRepository
from app.db.repositories.jobs import JobRepository
from app.schemas.enums import JobStatus


def cancel_run(
    session_scope: Any, *, job_id: str | None = None, batch_id: str | None = None
) -> JobStatus:
    """Flag every non-terminal cell (and the job/batch row itself)
    `cancel_requested`. Idempotent: cells already terminal are left alone.
    """
    if job_id is None and batch_id is None:
        raise ValueError("cancel_run requires job_id or batch_id")
    with session_scope() as session:
        CellRepository(session).request_cancel(job_id=job_id, batch_id=batch_id)
        JobRepository(session).request_cancel(job_id=job_id, batch_id=batch_id)
        return JobRepository(session).aggregate_status(job_id=job_id, batch_id=batch_id)


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
