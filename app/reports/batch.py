"""Batch creation + reconciliation (task-47 §2.2/§2.4).

A batch has no execution engine of its own. Every client in it is an
ordinary `ReportRun` (`app.reports.runner.run` — periods, `periods_json`,
events, XLSX, client PDF, cancellation, stale-run recovery all keep working
completely unchanged), created here with `batch_id` set and left for the
SAME scheduler pools task-42 already has (the pool split that gives a batch
its own concurrency lives in `app/reports/scheduler.py`, task-47 §2.3).

This module does only the two things a batch needs beyond "N runs": create
them all atomically under one parent row, and roll their statuses up into
that row once they finish. `ReportScheduler`'s own tick calls
`reconcile_batches` alongside its normal claim loop — a batch is an
aggregate, not a job with a code path of its own to hook a finish callback
into (task-42/45's `run()` has no idea a batch exists, by design).
"""

from __future__ import annotations

from typing import Any

from app.db.models import ReportBatch
from app.db.repositories.clients import ClientRepository
from app.db.repositories.reports import ReportRepository
from app.reports.periods import ReportPeriod
from app.reports.runner import build_periods, period_rows

_RUNNING_STATUSES = ("queued", "running")
_SUCCESSFUL_STATUSES = ("succeeded", "partial")


class NoClientsSelectedError(ValueError):
    """`client_ids` was empty (task-47 §5 `422 no_clients_selected`)."""


def create_batch(
    session: Any,
    *,
    client_ids: list[str],
    kind: str,
    source: str,
    params: dict[str, Any],
    max_months: int,
    max_comparator_years: int,
) -> ReportBatch:
    """Build the shared period list ONCE (it depends only on `kind`/`params`,
    never on the client) and fan it out into one `queued` run per client.
    Raises `InvalidPeriodError`/`InvalidComparatorMonthsError`/`ValueError`
    for a malformed request — the caller (`app/api/reports.py`) maps those to
    `422` exactly like `POST /reports/runs` already does.
    """
    if not client_ids:
        raise NoClientsSelectedError("At least one client must be selected")

    periods: list[ReportPeriod] = build_periods(
        kind, params, max_months=max_months, max_comparator_years=max_comparator_years
    )
    rows = period_rows(periods)

    repo = ReportRepository(session)
    client_repo = ClientRepository(session)
    batch = repo.create_batch(kind=kind, source=source, params=params, client_count=len(client_ids))
    created = 0
    for client_id in client_ids:
        # A client removed between the wizard loading its list and the
        # operator clicking Start is skipped, not fatal to the rest of the
        # batch — the same "one cell's failure never aborts the others"
        # isolation PLAN §9.2 already requires for every other batch shape.
        if client_repo.get(client_id) is None:
            continue
        repo.create_run(
            client_id, kind=kind, source=source, params=params,
            period_count=len(periods), periods=rows, batch_id=batch.id,
        )
        created += 1
    if created != len(client_ids):
        repo.update_batch_progress(batch.id, clients_done=0, clients_failed=0)
        batch.client_count = created
    return batch


def cancel_batch(session_scope: Any, batch_id: str) -> str | None:
    """Returns an error code (`"batch_not_found"`/`"batch_not_cancellable"`)
    or `None` on success. A still-`queued` child is cancelled immediately
    (task-47 DoD: "< 90s"); a `running` child gets the same cooperative
    `cancel_requested` flag a standalone run's cancel button already sets —
    its own loop notices at the next period boundary, unchanged from task-42.
    """
    with session_scope() as session:
        repo = ReportRepository(session)
        if repo.get_batch(batch_id) is None:
            return "batch_not_found"
        if repo.request_cancel_batch(batch_id) is None:
            return "batch_not_cancellable"
        for child in repo.list_runs_for_batch(batch_id):
            if child.status == "queued":
                repo.cancel_queued_run(child.id)
            elif child.status == "running":
                repo.request_cancel(child.id)
    return None


def _aggregate_batch_status(total: int, failed: int) -> str:
    if failed == 0:
        return "succeeded"
    if total > 0 and failed >= total:
        return "failed"
    return "partial"


def reconcile_batches(session_scope: Any) -> None:
    """Roll every non-terminal batch's children up into its own row. Cheap
    even at portfolio scale — a handful of batches, each with at most a few
    dozen children — so this runs on every scheduler tick (2s) rather than
    needing its own timer.
    """
    with session_scope() as session:
        batch_ids = ReportRepository(session).non_terminal_batch_ids()
    for batch_id in batch_ids:
        _reconcile_one(session_scope, batch_id)


def _reconcile_one(session_scope: Any, batch_id: str) -> None:
    with session_scope() as session:
        repo = ReportRepository(session)
        batch = repo.get_batch(batch_id)
        if batch is None:
            return
        children = repo.list_runs_for_batch(batch_id)
        if not children:
            return

        done = sum(1 for c in children if c.status in _SUCCESSFUL_STATUSES)
        failed = sum(1 for c in children if c.status == "failed")
        any_running = any(c.status in _RUNNING_STATUSES for c in children)
        repo.update_batch_progress(batch_id, clients_done=done, clients_failed=failed)

        if batch.status == "queued" and any(c.status not in ("queued",) for c in children):
            repo.start_batch(batch_id)

        if not any_running:
            # A cancelled batch is reported as cancelled overall even when
            # some clients finished before the cancel landed — the same
            # honesty `aggregate_run_status` already gives one standalone run
            # (task-42's own `is_cancelled()` short-circuit).
            status = "cancelled" if batch.cancel_requested else _aggregate_batch_status(
                len(children), failed
            )
            repo.finish_batch(batch_id, status=status)
