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

from app.authorship.model import Attribution
from app.db.models import ReportBatch, ReportRun
from app.db.repositories.clients import ClientRepository
from app.db.repositories.reports import ReportRepository
from app.reports.periods import ReportPeriod
from app.reports.runner import build_periods, period_rows

_RUNNING_STATUSES = ("queued", "running")
_SUCCESSFUL_STATUSES = ("succeeded", "partial")
_RETRYABLE_RUN_STATUSES = ("succeeded", "partial", "failed", "cancelled")


class NoClientsSelectedError(ValueError):
    """`client_ids` was empty (task-47 §5 `422 no_clients_selected`)."""


def latest_runs_by_client(children: list[ReportRun]) -> dict[str, ReportRun]:
    """One row per client — the batch's own current view (task-49 §2.4/§2.5).

    A batch-internal retry/rebuild creates a NEW `ReportRun` with the SAME
    `batch_id` rather than reusing the old row (task-49 explicitly needs no
    migration, so there is no `superseded_by`/`replaces` column to mark the
    old one with). `children` is already ordered by `created_at` ascending
    (`list_runs_for_batch`) — assigning into a plain dict as we walk it keeps
    each client's key at its FIRST-seen position (Python dicts don't move a
    key on re-assignment) while the VALUE ends up being its most recent run.
    That is exactly "replaces the previous one in the matrix": the old row
    simply stops being returned, with zero schema change.
    """
    latest: dict[str, ReportRun] = {}
    for child in children:
        latest[child.client_id] = child
    return latest


def rebuild_run(
    session_scope: Any,
    run_id: str,
    *,
    force_refresh: bool,
    max_months: int,
    max_comparator_years: int,
    created_by: Attribution | None = None,
) -> tuple[str | None, str | None]:
    """Clone `run_id` into a new `queued` run with the same `kind`/`source`/
    params, `force_refresh` overwritten EXPLICITLY (task-49 §1.4/§2.5 — the
    old "Retry" button silently kept `force_refresh: false` even when
    retrying because the data was wrong), preserving the source run's
    `batch_id` if it had one. One primitive serves both the standalone
    "Rebuild" action on a run page and the in-batch "Retry"/"Rebuild" pair
    (`app/api/reports.py`'s two new routes both call this).

    `created_by` (task-93 plan §2 D5): the new run's author is whoever
    clicked rebuild/retry NOW, never the source run's original author — a
    rebuild is a fresh decision to spend the collection budget again.

    Returns `(error_code, new_run_id)` — exactly one is `None`. Error codes:
    `run_not_found`, `run_not_retryable` (the source run hasn't finished yet).
    """
    with session_scope() as session:
        repo = ReportRepository(session)
        source = repo.get(run_id)
        if source is None:
            return "run_not_found", None
        if source.status not in _RETRYABLE_RUN_STATUSES:
            return "run_not_retryable", None

        params = dict(source.params_json or {})
        params["force_refresh"] = force_refresh
        periods = build_periods(
            source.kind, params,
            max_months=max_months, max_comparator_years=max_comparator_years,
        )
        new_run = repo.create_run(
            source.client_id, kind=source.kind, source=source.source, params=params,
            period_count=len(periods), periods=period_rows(periods),
            batch_id=source.batch_id, created_by=created_by,
        )

        if source.batch_id is not None:
            batch = repo.get_batch(source.batch_id)
            # A batch that already finished must be reopened, or
            # `reconcile_batches`' `non_terminal_batch_ids()` filter will
            # never look at it again and the new child would sit `queued`
            # forever from the batch's point of view (task-49 §2.4 point 1).
            # Direct ORM mutation, same convention already used above
            # (`batch.client_count = created`) — no new repository setter.
            if batch is not None and batch.status not in _RUNNING_STATUSES:
                batch.status = "running"
                batch.finished_at = None
                session.flush()

        return None, new_run.id


def create_batch(
    session: Any,
    *,
    client_ids: list[str],
    kind: str,
    source: str,
    params: dict[str, Any],
    max_months: int,
    max_comparator_years: int,
    created_by: Attribution | None = None,
) -> ReportBatch:
    """Build the shared period list ONCE (it depends only on `kind`/`params`,
    never on the client) and fan it out into one `queued` run per client.
    Raises `InvalidPeriodError`/`InvalidComparatorMonthsError`/`ValueError`
    for a malformed request — the caller (`app/api/reports.py`) maps those to
    `422` exactly like `POST /reports/runs` already does.

    `created_by` (task-93 plan §2 D5) is written on the batch AND on every
    child run it fans out — whoever started the batch is the author of the
    whole thing, not just the parent row.
    """
    if not client_ids:
        raise NoClientsSelectedError("At least one client must be selected")

    periods: list[ReportPeriod] = build_periods(
        kind, params, max_months=max_months, max_comparator_years=max_comparator_years
    )
    rows = period_rows(periods)

    repo = ReportRepository(session)
    client_repo = ClientRepository(session)
    batch = repo.create_batch(
        kind=kind, source=source, params=params, client_count=len(client_ids),
        created_by=created_by,
    )
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
            created_by=created_by,
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

        # task-49 §2.4: count each client's LATEST run only, never a run a
        # retry/rebuild has since replaced — otherwise a retried client would
        # be double-counted (its old failed row plus its new one) and the
        # batch could never reach `succeeded` again.
        current = list(latest_runs_by_client(children).values())
        done = sum(1 for c in current if c.status in _SUCCESSFUL_STATUSES)
        failed = sum(1 for c in current if c.status == "failed")
        any_running = any(c.status in _RUNNING_STATUSES for c in current)
        repo.update_batch_progress(batch_id, clients_done=done, clients_failed=failed)

        if batch.status == "queued" and any(c.status not in ("queued",) for c in current):
            repo.start_batch(batch_id)

        if not any_running:
            # A cancelled batch is reported as cancelled overall even when
            # some clients finished before the cancel landed — the same
            # honesty `aggregate_run_status` already gives one standalone run
            # (task-42's own `is_cancelled()` short-circuit).
            status = "cancelled" if batch.cancel_requested else _aggregate_batch_status(
                len(current), failed
            )
            repo.finish_batch(batch_id, status=status)
