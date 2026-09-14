"""Report repository (`report_runs`/`report_run_events`/`report_files`, task-42 §4).

Same conventions as `app/db/repositories/monitor.py`: constructed directly
with a plain `Session`, every mutating method `flush()`es and leaves
`commit()` to the caller's transaction boundary (the scheduler's tick, or the
API's own `worker_session_scope()`).
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.authorship.model import Attribution
from app.db.base import utcnow
from app.db.models import ReportBatch, ReportFile, ReportRun, ReportRunEvent

_TERMINAL_STATUSES = ("succeeded", "partial", "failed", "cancelled")

# Per-period states (task-45 §2.1). `cached` is deliberately its own terminal
# state, not a flavour of `done`: on the PDF path it is the difference between
# 0 and ~60 seconds of browser work, and the operator has to see which it was.
PERIOD_TERMINAL_STATES = ("done", "cached", "failed", "skipped")
PERIOD_PENDING_STATES = ("waiting", "running")


def _elapsed_seconds(started_at: Any, now: datetime) -> float | None:
    """Seconds between a stored ISO `started_at` and `now`, or `None` when the
    period never started (a cancelled `waiting` period has no duration — `0.0`
    would read as "took no time", which is not the same thing, PLAN §12).
    """
    if not isinstance(started_at, str):
        return None
    try:
        started = datetime.fromisoformat(started_at)
    except ValueError:
        return None
    return round((now - started).total_seconds(), 3)


def apply_authorship_filter(
    stmt: Any, model: Any, *, created_by_user_id: str | None, created_by_kind: str | None
) -> Any:
    """Shared by every list query with a `created_by_*` filter (task-93 plan
    §2 D4): `list_runs`/`list_batches` here, `ContentQaPackageRepository.
    list_packages` (`app/db/repositories/content_qa_packages.py`). Every
    target model carries the same two column names, so one function serves
    all three — the caller (an admin-only route) is responsible for the 403
    on every non-admin request that even sets these, per plan §2 D4's "a
    filter is an oracle" rule; this function only ever narrows a query,
    never decides who may call it.

    `created_by_kind="unknown"` is not a real value stored in the column —
    it means "rows with no recorded author at all" (`NULL`, never a literal
    string), the honest state for anything created before task-93 or left
    ambiguous by the backfill.
    """
    if created_by_kind == "unknown":
        stmt = stmt.where(model.created_by_kind.is_(None))
    elif created_by_kind is not None:
        stmt = stmt.where(model.created_by_kind == created_by_kind)
    if created_by_user_id is not None:
        stmt = stmt.where(model.created_by_user_id == created_by_user_id)
    return stmt


class ReportRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def commit(self) -> None:
        self._session.commit()

    # --- runs -----------------------------------------------------------

    def create_run(
        self,
        client_id: str,
        *,
        kind: str,
        source: str,
        params: dict[str, Any],
        period_count: int,
        periods: list[dict[str, Any]] | None = None,
        batch_id: str | None = None,
        created_by: Attribution | None = None,
    ) -> ReportRun:
        """`periods` (task-45 §2.1) is the full per-period list, every entry
        `waiting` with its dates already resolved — passed at CREATE time so
        the UI shows a complete, date-labelled period list from the first
        second of a `queued` run, before the worker has even claimed it.
        `batch_id` (task-47 §2.2) makes this run a batch's child — `None` for
        every standalone run, unchanged from task-42.

        `created_by` (task-93): who is responsible for THIS run existing —
        for a rebuild/retry that's whoever clicked the button, not the
        source run's original author (plan §2 D5). `None` (the default)
        means "unknown", written as `NULL` in both columns, never guessed.
        """
        run = ReportRun(
            client_id=client_id,
            kind=kind,
            source=source,
            params_json=params,
            period_count=period_count,
            periods_json=list(periods) if periods is not None else None,
            batch_id=batch_id,
            created_by_user_id=created_by.user_id if created_by else None,
            created_by_kind=created_by.kind if created_by else None,
        )
        self._session.add(run)
        self._session.flush()
        return run

    def get(self, run_id: str) -> ReportRun | None:
        return self._session.get(ReportRun, run_id)

    def list_runs(
        self,
        *,
        client_id: str | None = None,
        kind: str | None = None,
        source: str | None = None,
        status: str | None = None,
        has_batch: bool | None = None,
        created_by_user_id: str | None = None,
        created_by_kind: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[ReportRun], int]:
        stmt = select(ReportRun)
        if client_id is not None:
            stmt = stmt.where(ReportRun.client_id == client_id)
        if kind is not None:
            stmt = stmt.where(ReportRun.kind == kind)
        if source is not None:
            stmt = stmt.where(ReportRun.source == source)
        if status is not None:
            stmt = stmt.where(ReportRun.status == status)
        # `Scope` filter (task-47 §3.5 point 6): `Single runs` hides every
        # batch child so a 14-client batch doesn't drown the history in
        # near-identical rows; `Batches` is served by `list_batches` instead.
        if has_batch is True:
            stmt = stmt.where(ReportRun.batch_id.is_not(None))
        elif has_batch is False:
            stmt = stmt.where(ReportRun.batch_id.is_(None))
        stmt = apply_authorship_filter(
            stmt, ReportRun, created_by_user_id=created_by_user_id, created_by_kind=created_by_kind
        )

        total = self._session.scalar(select(func.count()).select_from(stmt.subquery())) or 0
        stmt = stmt.order_by(ReportRun.created_at.desc()).limit(limit).offset(offset)
        rows = list(self._session.scalars(stmt))
        return rows, total

    def claim_next_queued(
        self, source: str, *, lease_owner: str, batch_only: bool | None = None
    ) -> ReportRun | None:
        """Atomically claim the oldest `queued` run of one `source` for this worker.

        Mirrors `MonitorScheduler`'s claim-then-commit shape: the caller holds
        the process-wide write lock across this call and its `commit()`, so
        the `SELECT` + `UPDATE` pair is safe without a DB-level `FOR UPDATE`
        (SQLite has no row locking; the lock IS the concurrency control).

        `batch_only` (task-47 §2.3) splits the API-path queue in two: a batch
        monopolizing every `reports_api_workers` slot would otherwise queue an
        unrelated ad-hoc single-client report behind the whole batch.
        """
        stmt = select(ReportRun).where(ReportRun.source == source, ReportRun.status == "queued")
        if batch_only is True:
            stmt = stmt.where(ReportRun.batch_id.is_not(None))
        elif batch_only is False:
            stmt = stmt.where(ReportRun.batch_id.is_(None))
        stmt = stmt.order_by(ReportRun.queued_at).limit(1)
        run = self._session.scalars(stmt).first()
        if run is None:
            return None
        run.status = "running"
        run.lease_owner = lease_owner
        run.started_at = utcnow()
        run.heartbeat_at = utcnow()
        self._session.flush()
        return run

    def cancel_queued_run(self, run_id: str) -> bool:
        """A still-`queued` batch child never started, so cancelling it is
        immediate — no `is_cancelled()` check to wait for (task-47 §2.2:
        "Скасування батчу … незапущені лишаються скасованими" / DoD "< 90s").
        """
        run = self._session.get(ReportRun, run_id)
        if run is None or run.status != "queued":
            return False
        run.status = "cancelled"
        run.cancel_requested = True
        run.finished_at = utcnow()
        self._session.flush()
        return True

    def heartbeat(self, run_id: str, *, lease_owner: str) -> None:
        run = self._session.get(ReportRun, run_id)
        if run is None:
            return
        run.lease_owner = lease_owner
        run.heartbeat_at = utcnow()
        self._session.flush()

    def update_progress(
        self,
        run_id: str,
        *,
        periods_done: int | None = None,
        periods_failed: int | None = None,
    ) -> None:
        run = self._session.get(ReportRun, run_id)
        if run is None:
            return
        if periods_done is not None:
            run.periods_done = periods_done
        if periods_failed is not None:
            run.periods_failed = periods_failed
        self._session.flush()

    # --- per-period progress (task-45 §2.1) --------------------------------
    #
    # Every write reassigns `periods_json` wholesale — a plain `JSON` column is
    # not a `MutableList`, so mutating a nested dict in place would never be
    # flushed. Each of these runs inside the caller's own short transaction,
    # exactly like `update_progress`, so progress stays continuously persisted
    # (PLAN §9.2) and survives F5 or a worker restart.

    def set_periods(self, run_id: str, periods: list[dict[str, Any]]) -> None:
        run = self._session.get(ReportRun, run_id)
        if run is None:
            return
        run.periods_json = [dict(period) for period in periods]
        self._session.flush()

    def update_period(
        self,
        run_id: str,
        label: str,
        *,
        state: str,
        from_cache: bool | None = None,
        error: str | None = None,
        attempt: int | None = None,
        max_attempts: int | None = None,
    ) -> None:
        """Move one period to `state`, stamping the timestamps that state
        implies: `running` starts the clock, any terminal state stops it and
        records `duration_seconds`.

        `attempt`/`max_attempts` (additive, task-47) let the UI show "Running
        — retry 2/3" / "Failed (3/3 attempts)" instead of a bare state word.
        Only `attempt <= 1` resets `started_at` — a retry re-enters `running`
        with `attempt > 1` and KEEPS the original start time, so a period
        that failed after 3 attempts reports how long the whole ordeal took,
        not just the last one.
        """
        run = self._session.get(ReportRun, run_id)
        if run is None or not run.periods_json:
            return
        now = utcnow()
        rows = [dict(row) for row in run.periods_json]
        for row in rows:
            if row.get("label") != label:
                continue
            row["state"] = state
            if state == "running":
                if not attempt or attempt <= 1:
                    row["started_at"] = now.isoformat()
                row["finished_at"] = None
                row["duration_seconds"] = None
            elif state in PERIOD_TERMINAL_STATES:
                row["finished_at"] = now.isoformat()
                row["duration_seconds"] = _elapsed_seconds(row.get("started_at"), now)
            if from_cache is not None:
                row["from_cache"] = from_cache
            if error is not None:
                row["error"] = error[:400]
            if attempt is not None:
                row["attempt"] = attempt
            if max_attempts is not None:
                row["max_attempts"] = max_attempts
        run.periods_json = rows
        self._session.flush()

    def mark_pending_periods(self, run_id: str, *, state: str) -> None:
        """Everything still `waiting`/`running` becomes `state` — used when a
        run is cancelled, so the remaining periods read `Skipped` instead of
        forever `Waiting` (task-45 §5).
        """
        run = self._session.get(ReportRun, run_id)
        if run is None or not run.periods_json:
            return
        now = utcnow()
        rows = []
        for source_row in run.periods_json:
            row = dict(source_row)
            if row.get("state") in PERIOD_PENDING_STATES:
                row["state"] = state
                row["finished_at"] = now.isoformat()
                row["duration_seconds"] = _elapsed_seconds(row.get("started_at"), now)
            rows.append(row)
        run.periods_json = rows
        self._session.flush()

    def finish(
        self,
        run_id: str,
        *,
        status: str,
        result: dict[str, Any] | None = None,
        cross_check_status: str | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> ReportRun | None:
        run = self._session.get(ReportRun, run_id)
        if run is None:
            return None
        run.status = status
        if result is not None:
            run.result_json = result
        if cross_check_status is not None:
            run.cross_check_status = cross_check_status
        run.error_code = error_code
        run.error_message = error_message
        run.finished_at = utcnow()
        self._session.flush()
        return run

    def request_cancel(self, run_id: str) -> ReportRun | None:
        run = self._session.get(ReportRun, run_id)
        if run is None or run.status in _TERMINAL_STATUSES:
            return None
        run.cancel_requested = True
        self._session.flush()
        return run

    def recover_stale_running(self) -> int:
        """Startup recovery (task-42 §9 DoD): a `running` row with no live
        owner is, by definition, orphaned in a single-writer worker (PLAN
        §5.1 — exactly one `worker` instance ever runs). Called ONCE, before
        the scheduler's dispatcher claims any new work, so every `running`
        row found here predates this process — there is no "maybe it's still
        legitimately in progress" case to protect with a staleness window.

        Live bug (2026-08-19): this used to gate on `stale_minutes` (default
        60) before recovering a row, so a run whose worker died seconds after
        writing `heartbeat_at` sat "running" — and its `Cancel` button
        inert, since nothing was polling `cancel_requested` any more — for
        up to an hour. Mirrors `MonitorScheduler._recover_stale_runs`, which
        never had this gate.
        """
        stmt = select(ReportRun).where(ReportRun.status == "running")
        recovered = 0
        for run in self._session.scalars(stmt):
            run.status = "failed"
            run.error_code = "interrupted"
            run.error_message = "Worker restarted while this run was in progress"
            run.finished_at = utcnow()
            self.mark_pending_periods(run.id, state="skipped")
            recovered += 1
        self._session.flush()
        return recovered

    # --- events -----------------------------------------------------------

    def append_event(
        self,
        run_id: str,
        level: str,
        *,
        stage: str | None = None,
        message: str = "",
        details: dict[str, Any] | None = None,
    ) -> int:
        next_seq = (
            self._session.scalar(
                select(func.coalesce(func.max(ReportRunEvent.seq), 0)).where(
                    ReportRunEvent.run_id == run_id
                )
            )
            or 0
        ) + 1
        event = ReportRunEvent(
            run_id=run_id,
            seq=next_seq,
            level=level,
            stage=stage,
            message=message,
            details_json=details or {},
        )
        self._session.add(event)
        self._session.flush()
        return next_seq

    def events_since(self, run_id: str, last_seq: int) -> list[ReportRunEvent]:
        stmt = (
            select(ReportRunEvent)
            .where(ReportRunEvent.run_id == run_id, ReportRunEvent.seq > last_seq)
            .order_by(ReportRunEvent.seq)
        )
        return list(self._session.scalars(stmt))

    # --- files --------------------------------------------------------------

    def find_cached_file(
        self,
        client_id: str,
        source_report_name: str,
        date_from: date,
        date_to: date,
    ) -> ReportFile | None:
        stmt = (
            select(ReportFile)
            .where(
                ReportFile.client_id == client_id,
                ReportFile.source_report_name == source_report_name,
                ReportFile.date_from == date_from,
                ReportFile.date_to == date_to,
                ReportFile.kind == "pdf",
            )
            .order_by(ReportFile.created_at.desc())
        )
        return self._session.scalars(stmt).first()

    def create_file(
        self,
        client_id: str,
        *,
        run_id: str | None,
        kind: str,
        path: str,
        sha256: str,
        size_bytes: int,
        mime: str | None = None,
        period_label: str | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
        is_partial_period: bool = False,
        source_report_name: str | None = None,
    ) -> ReportFile:
        file = ReportFile(
            client_id=client_id,
            run_id=run_id,
            kind=kind,
            path=path,
            sha256=sha256,
            size_bytes=size_bytes,
            mime=mime,
            period_label=period_label,
            date_from=date_from,
            date_to=date_to,
            is_partial_period=is_partial_period,
            source_report_name=source_report_name,
        )
        self._session.add(file)
        self._session.flush()
        return file

    def get_file(self, file_id: str) -> ReportFile | None:
        return self._session.get(ReportFile, file_id)

    def list_files_for_run(self, run_id: str) -> list[ReportFile]:
        stmt = select(ReportFile).where(ReportFile.run_id == run_id).order_by(ReportFile.created_at)
        return list(self._session.scalars(stmt))

    # --- batches (task-47 §2.2/§4) ------------------------------------------

    def create_batch(
        self,
        *,
        kind: str,
        source: str,
        params: dict[str, Any],
        client_count: int,
        created_by: Attribution | None = None,
    ) -> ReportBatch:
        batch = ReportBatch(
            kind=kind,
            source=source,
            params_json=params,
            client_count=client_count,
            created_by_user_id=created_by.user_id if created_by else None,
            created_by_kind=created_by.kind if created_by else None,
        )
        self._session.add(batch)
        self._session.flush()
        return batch

    def get_batch(self, batch_id: str) -> ReportBatch | None:
        return self._session.get(ReportBatch, batch_id)

    def list_batches(
        self, *, client_id: str | None = None, status: str | None = None,
        created_by_user_id: str | None = None, created_by_kind: str | None = None,
        limit: int = 100, offset: int = 0,
    ) -> tuple[list[ReportBatch], int]:
        stmt = select(ReportBatch)
        if status is not None:
            stmt = stmt.where(ReportBatch.status == status)
        if client_id is not None:
            # A batch "contains" a client filter (task-47 §3.5 point 7):
            # never hide a batch a client is genuinely part of just because
            # the client filter was built for single runs.
            stmt = stmt.where(
                ReportBatch.id.in_(
                    select(ReportRun.batch_id).where(
                        ReportRun.client_id == client_id, ReportRun.batch_id.is_not(None)
                    )
                )
            )
        stmt = apply_authorship_filter(
            stmt, ReportBatch,
            created_by_user_id=created_by_user_id, created_by_kind=created_by_kind,
        )
        total = self._session.scalar(select(func.count()).select_from(stmt.subquery())) or 0
        stmt = stmt.order_by(ReportBatch.created_at.desc()).limit(limit).offset(offset)
        rows = list(self._session.scalars(stmt))
        return rows, total

    def list_runs_for_batch(self, batch_id: str) -> list[ReportRun]:
        stmt = (
            select(ReportRun)
            .where(ReportRun.batch_id == batch_id)
            .order_by(ReportRun.created_at)
        )
        return list(self._session.scalars(stmt))

    def start_batch(self, batch_id: str) -> None:
        batch = self._session.get(ReportBatch, batch_id)
        if batch is None or batch.status != "queued":
            return
        batch.status = "running"
        batch.started_at = utcnow()
        self._session.flush()

    def update_batch_progress(
        self, batch_id: str, *, clients_done: int, clients_failed: int
    ) -> None:
        batch = self._session.get(ReportBatch, batch_id)
        if batch is None:
            return
        batch.clients_done = clients_done
        batch.clients_failed = clients_failed
        self._session.flush()

    def finish_batch(self, batch_id: str, *, status: str) -> None:
        batch = self._session.get(ReportBatch, batch_id)
        if batch is None:
            return
        batch.status = status
        batch.finished_at = utcnow()
        self._session.flush()

    def request_cancel_batch(self, batch_id: str) -> ReportBatch | None:
        batch = self._session.get(ReportBatch, batch_id)
        if batch is None or batch.status in _TERMINAL_STATUSES:
            return None
        batch.cancel_requested = True
        self._session.flush()
        return batch

    def non_terminal_batch_ids(self) -> list[str]:
        stmt = select(ReportBatch.id).where(ReportBatch.status.not_in(_TERMINAL_STATUSES))
        return list(self._session.scalars(stmt))
