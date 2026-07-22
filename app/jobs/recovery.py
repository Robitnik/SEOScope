"""Startup recovery (PLAN §9.2/§9.3): no cell is ever left "running" forever
after a worker restart, and nothing that was mid-flight when the process
died is silently lost or silently retried at the cost of double-billing a
metered API.

Runs once, before the dispatcher starts claiming new work. Both cases below
only ever touch cells whose owning lane is provably gone (this process just
started, so nothing from a previous instance can still be alive):

- `running` with an expired lease: the previous process died or was killed
  mid-collection. `runner.run_cell` never writes a snapshot/metrics until
  its very last step, so nothing partial was committed — it is always safe
  to requeue for an automatic, idempotent retry (PLAN §9.2 "retried
  idempotently"), after first recording the interruption as
  `failed_interrupted` for the audit trail.
- `cancel_requested` left over from a previous instance: no lane is watching
  it anymore, so it is finalized straight to `cancelled` instead of being
  silently reclaimable as `queued` again.
- `awaiting_auth` left over from a previous instance (task-21): no lane is
  polling it anymore either — the pause loop that would resume it died with
  the process — so it is requeued unconditionally; the new dispatcher's
  first claim for that provider re-runs the cheap session check and either
  proceeds or re-pauses honestly.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from app.db.repositories.cells import CellRepository
from app.db.repositories.events import EventRepository
from app.db.repositories.jobs import JobRepository
from app.schemas.enums import CellStatus

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RecoveryReport:
    requeued_interrupted: int
    finalized_cancelled: int
    requeued_awaiting_auth: int = 0


def run_startup_recovery(session_scope: Any) -> RecoveryReport:
    requeued = 0
    finalized = 0
    requeued_auth = 0

    with session_scope() as session:
        cells = CellRepository(session)
        events = EventRepository(session)
        jobs = JobRepository(session)

        for cell in cells.expired_cells():
            cells.set_status(
                cell.id, CellStatus.failed_interrupted, error_code="interrupted_by_restart"
            )
            events.append(
                cell.job_id,
                "warning",
                provider=cell.provider,
                stage="recovery",
                message="cell interrupted by worker restart; auto-requeued",
            )
            cells.requeue_for_retry(cell.id)
            jobs.sync_status_from_cells(cell.job_id)
            requeued += 1
            logger.warning(
                "recovery_requeued_interrupted_cell cell_id=%s provider=%s", cell.id, cell.provider
            )

        for cell in cells.stale_cancel_requested_cells():
            cells.set_status(cell.id, CellStatus.cancelled)
            events.append(
                cell.job_id,
                "info",
                provider=cell.provider,
                stage="recovery",
                message="cell finalized cancelled after worker restart",
            )
            jobs.sync_status_from_cells(cell.job_id)
            finalized += 1
            logger.info(
                "recovery_finalized_cancelled_cell cell_id=%s provider=%s", cell.id, cell.provider
            )

        for cell in cells.awaiting_auth_cells():
            cells.set_status(cell.id, CellStatus.queued, error_code=None, error_message=None)
            events.append(
                cell.job_id,
                "info",
                provider=cell.provider,
                stage="recovery",
                message="auth-pause cleared by worker restart; requeued for re-check",
            )
            jobs.sync_status_from_cells(cell.job_id)
            requeued_auth += 1
            logger.info(
                "recovery_requeued_awaiting_auth_cell cell_id=%s provider=%s",
                cell.id,
                cell.provider,
            )

    report = RecoveryReport(
        requeued_interrupted=requeued,
        finalized_cancelled=finalized,
        requeued_awaiting_auth=requeued_auth,
    )
    logger.info(
        "recovery_complete requeued_interrupted=%d finalized_cancelled=%d "
        "requeued_awaiting_auth=%d",
        report.requeued_interrupted,
        report.finalized_cancelled,
        report.requeued_awaiting_auth,
    )
    return report
