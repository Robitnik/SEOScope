"""Cell repository: atomic claim/lease/status for one `(client, provider)` cell
(`sync_job_providers`, PLAN §9.1/§9.2).

SQLite has no `SELECT ... FOR UPDATE SKIP LOCKED`; atomicity instead comes
from a conditional `UPDATE ... WHERE id = :id AND status = 'queued'` whose
row count proves whether *this* call won the claim — safe under SQLite's
single-writer model (PLAN §5.1) even when called from several lanes in the
same process.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import timedelta
from typing import Any, cast

from sqlalchemy import CursorResult, func, select, update
from sqlalchemy.orm import Session, aliased

from app.db.base import utcnow
from app.db.models import SyncJobProvider
from app.schemas.enums import CellStatus, Provider


class CellRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def claim_next_cell(
        self,
        provider: Provider,
        *,
        lease_owner: str,
        lease_ttl_seconds: int = 300,
        profile_capacity: int = 1,
        slot: int | None = None,
    ) -> SyncJobProvider | None:
        """Claim the oldest queued cell for `provider`, or `None`.

        `profile_capacity` (task-31) turns the profile lock from a mutex into a
        counting semaphore: up to N cells sharing one `profile_key` may be
        `running` at once — N browser windows driven on one logged-in profile.
        `profile_capacity=1` is exactly the previous behaviour.

        The capacity check lives INSIDE the claim `UPDATE`, never as a separate
        `SELECT` before it: two lanes reading "count < N" in the same instant
        would both proceed and overshoot. As one statement, SQLite's single
        writer (PLAN §5.1) serialises them, and `rowcount` proves which one won
        — the same argument this module's own atomicity already rests on.
        """
        now = utcnow()
        stmt = (
            select(SyncJobProvider)
            .where(
                SyncJobProvider.provider == provider, SyncJobProvider.status == CellStatus.queued
            )
            .order_by(SyncJobProvider.created_at)
        )
        for cell in self._session.scalars(stmt):
            if cell.profile_key is not None and self.profile_slots_free(
                cell.profile_key, capacity=profile_capacity
            ) <= 0:
                continue

            claim_conditions = [
                SyncJobProvider.id == cell.id,
                SyncJobProvider.status == CellStatus.queued,
            ]
            if cell.profile_key is not None:
                holder = aliased(SyncJobProvider)
                claim_conditions.append(
                    select(func.count())
                    .select_from(holder)
                    .where(
                        holder.profile_key == cell.profile_key,
                        holder.status == CellStatus.running,
                    )
                    .scalar_subquery()
                    < profile_capacity
                )

            result = cast(
                "CursorResult[Any]",
                self._session.execute(
                    update(SyncJobProvider)
                    .where(*claim_conditions)
                    .values(
                        status=CellStatus.running,
                        lease_owner=lease_owner,
                        lease_expires_at=now + timedelta(seconds=lease_ttl_seconds),
                        heartbeat_at=now,
                        started_at=cell.started_at or now,
                        slot=slot,
                    )
                ),
            )
            if result.rowcount == 1:
                self._session.flush()
                self._session.refresh(cell)
                return cell
        return None

    def acquire_profile_lock(self, profile_key: str) -> bool:
        """True if no cell is currently `running` while holding this profile key.

        The single-slot question, kept for callers that mean exactly that;
        `profile_slots_free` is the general form used by the claim above.
        """
        return self.profile_slots_free(profile_key, capacity=1) > 0

    def profile_slots_free(self, profile_key: str, *, capacity: int = 1) -> int:
        """How many more cells may run on `profile_key` right now (never < 0)."""
        running = (
            self._session.scalar(
                select(func.count())
                .select_from(SyncJobProvider)
                .where(
                    SyncJobProvider.profile_key == profile_key,
                    SyncJobProvider.status == CellStatus.running,
                )
            )
            or 0
        )
        return max(0, capacity - int(running))

    def release_profile_lock(self, cell_id: str) -> None:
        """No-op beyond documentation: a lock frees the instant its cell leaves
        `running` via `set_status`. Kept as an explicit call site for callers
        that want to make the release point visible in their own code.
        """
        return None

    def heartbeat(self, cell_id: str, *, lease_owner: str, lease_ttl_seconds: int = 300) -> None:
        now = utcnow()
        self._session.execute(
            update(SyncJobProvider)
            .where(SyncJobProvider.id == cell_id, SyncJobProvider.lease_owner == lease_owner)
            .values(heartbeat_at=now, lease_expires_at=now + timedelta(seconds=lease_ttl_seconds))
        )
        self._session.flush()

    def set_status(
        self,
        cell_id: str,
        status: CellStatus,
        *,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> SyncJobProvider:
        cell = self._session.get(SyncJobProvider, cell_id)
        if cell is None:
            raise LookupError(f"cell not found: {cell_id}")
        cell.status = status
        cell.error_code = error_code
        cell.error_message = error_message
        if status in (
            CellStatus.succeeded,
            CellStatus.partial,
            CellStatus.failed,
            CellStatus.failed_interrupted,
            CellStatus.not_configured,
            CellStatus.skipped,
            CellStatus.cancelled,
        ):
            cell.finished_at = utcnow()
            cell.progress_percent = 100.0
            cell.current_stage = "complete"
            if status in (CellStatus.succeeded, CellStatus.partial):
                cell.last_success_at = utcnow()
        self._session.flush()
        return cell

    def set_progress(
        self,
        cell_id: str,
        progress_percent: float | None,
        *,
        current_stage: str | None = None,
        progress_message: str | None = None,
    ) -> None:
        cell = self._session.get(SyncJobProvider, cell_id)
        if cell is None:
            raise LookupError(f"cell not found: {cell_id}")
        if progress_percent is not None:
            cell.progress_percent = progress_percent
        if current_stage is not None:
            cell.current_stage = current_stage
        if progress_message is not None:
            cell.progress_message = progress_message
        self._session.flush()

    def expired_cells(self, *, now: object | None = None) -> list[SyncJobProvider]:
        """Cells still `running` whose lease has expired — startup recovery (PLAN §9.2)."""
        check_time = now or utcnow()
        stmt = select(SyncJobProvider).where(
            SyncJobProvider.status == CellStatus.running,
            SyncJobProvider.lease_expires_at.is_not(None),
            SyncJobProvider.lease_expires_at < check_time,
        )
        return list(self._session.scalars(stmt))

    def stale_cancel_requested_cells(self) -> list[SyncJobProvider]:
        """`cancel_requested` cells left over from a previous process — since
        this only runs once at worker startup (no lane can possibly still be
        watching them), they are safe to finalize unconditionally, unlike
        `expired_cells` which still checks a lease TTL (PLAN §9.2 recovery).
        """
        stmt = select(SyncJobProvider).where(SyncJobProvider.status == CellStatus.cancel_requested)
        return list(self._session.scalars(stmt))

    def failed_cells(
        self, *, job_id: str | None = None, batch_id: str | None = None
    ) -> list[SyncJobProvider]:
        if job_id is None and batch_id is None:
            raise ValueError("failed_cells requires job_id or batch_id")
        stmt = select(SyncJobProvider).where(
            SyncJobProvider.status.in_([CellStatus.failed, CellStatus.failed_interrupted])
        )
        stmt = stmt.where(SyncJobProvider.job_id == job_id) if job_id else stmt.where(
            SyncJobProvider.batch_id == batch_id
        )
        return list(self._session.scalars(stmt))

    # --- Added by task-05 (job engine): narrowly-scoped reads/writes the
    # dispatcher/runner need that no other repository method covered. See
    # this task's Completion report, "Contract changes", for the rationale.

    def get(self, cell_id: str) -> SyncJobProvider | None:
        return self._session.get(SyncJobProvider, cell_id)

    def list_for_job(self, job_id: str) -> list[SyncJobProvider]:
        stmt = select(SyncJobProvider).where(SyncJobProvider.job_id == job_id)
        return list(self._session.scalars(stmt))

    def requeue_for_retry(self, cell_id: str) -> SyncJobProvider:
        """Reset one `failed`/`failed_interrupted` cell to `queued` for a new
        attempt (PLAN §9.2 "retry failed only") — successful cells are never
        touched by this call.
        """
        cell = self._session.get(SyncJobProvider, cell_id)
        if cell is None:
            raise LookupError(f"cell not found: {cell_id}")
        if cell.status not in (CellStatus.failed, CellStatus.failed_interrupted):
            raise ValueError(f"cannot retry cell in status {cell.status!r}: {cell_id}")
        cell.status = CellStatus.queued
        cell.attempt += 1
        # A retry is claimed afresh and may land in a different lane; carrying
        # the old slot over would name a lane this attempt never ran in.
        cell.slot = None
        cell.lease_owner = None
        cell.lease_expires_at = None
        cell.heartbeat_at = None
        cell.progress_percent = 0.0
        cell.current_stage = None
        cell.error_code = None
        cell.error_message = None
        cell.started_at = None
        cell.finished_at = None
        self._session.flush()
        return cell

    # --- Added by task-06 (REST API): the dashboard/providers-status
    # endpoints need the most recent attempt for a `(client, provider)` pair
    # to distinguish `failed`/`auth_required`/in-flight from "configured but
    # never attempted" when there is no successful snapshot yet — no
    # existing method here is scoped to "latest cell regardless of job".
    # See this task's Completion report, "Contract changes", for the rationale.

    def latest_for_client_provider(
        self, client_id: str, provider: Provider
    ) -> SyncJobProvider | None:
        stmt = (
            select(SyncJobProvider)
            .where(SyncJobProvider.client_id == client_id, SyncJobProvider.provider == provider)
            .order_by(SyncJobProvider.created_at.desc())
        )
        return self._session.scalars(stmt).first()

    def latest_for_client_providers(
        self, pairs: Sequence[tuple[str, Provider]]
    ) -> dict[tuple[str, Provider], SyncJobProvider]:
        """Batched `latest_for_client_provider` (task-68 §3): one query for
        every `(client_id, provider)` pair instead of one call per pair —
        live-measured 123 individual calls (`GET /api/v1/dashboard`, 15
        clients x up to 5 providers each) at ~0.11-0.25ms apiece, cheap
        per call but 15-30ms of pure round-trip overhead added up.

        `ROW_NUMBER() OVER (PARTITION BY client_id, provider ORDER BY
        created_at DESC)`, same technique as
        `AnalyticsDatasetRepository.latest_rows_by_dimension` (task-68 §2) —
        this table's own PARTITION key is a 2-tuple instead of a single
        column, otherwise identical: rank within each partition, keep rank 1.
        A missing pair (no cell for that client/provider yet, e.g. a
        never-configured integration) is simply absent from the returned
        dict — callers already treat a missing entry the same as the
        single-pair method's `None` return.
        """
        if not pairs:
            return {}
        client_ids = {client_id for client_id, _ in pairs}
        providers = {provider for _, provider in pairs}
        row_number = (
            func.row_number()
            .over(
                partition_by=(SyncJobProvider.client_id, SyncJobProvider.provider),
                order_by=SyncJobProvider.created_at.desc(),
            )
            .label("rn")
        )
        subquery = (
            select(SyncJobProvider, row_number)
            .where(
                SyncJobProvider.client_id.in_(client_ids),
                SyncJobProvider.provider.in_(providers),
            )
            .subquery()
        )
        newest = aliased(SyncJobProvider, subquery)
        rows = self._session.scalars(select(newest).where(subquery.c.rn == 1))
        wanted = set(pairs)
        return {
            (row.client_id, row.provider): row
            for row in rows
            if (row.client_id, row.provider) in wanted
        }

    def list_for_batch(self, batch_id: str) -> list[SyncJobProvider]:
        stmt = select(SyncJobProvider).where(SyncJobProvider.batch_id == batch_id)
        return list(self._session.scalars(stmt))

    # --- Added by task-21 (session preflight / auth pause): the lane needs
    # to close out an entire dead-session backlog on pause-timeout, and
    # startup recovery needs to find cells stranded in `awaiting_auth` by a
    # restart — neither is expressible with the methods above. See this
    # task's Completion report, "Contract changes", for the rationale.

    def queued_cells_for_provider(self, provider: Provider) -> list[SyncJobProvider]:
        """Every still-`queued` cell for `provider`, across all jobs — used to
        close out the rest of a paused lane's backlog once its auth-pause
        times out, instead of each one re-pausing for another full timeout.
        """
        stmt = select(SyncJobProvider).where(
            SyncJobProvider.provider == provider, SyncJobProvider.status == CellStatus.queued
        )
        return list(self._session.scalars(stmt))

    def awaiting_auth_cells(self, provider: Provider | None = None) -> list[SyncJobProvider]:
        """`awaiting_auth` cells left over from a previous process — no lane
        from a previous instance can still be polling them after a restart,
        so they are always safe to requeue unconditionally (mirrors
        `stale_cancel_requested_cells`).

        `provider` (task-31) narrows this to one provider's parked cells: with
        several lanes per provider, more than one cell can be parked on the
        same dead session, and the lane that wins the auth gate has to resolve
        all of them — not just the one it happened to be holding.
        """
        stmt = select(SyncJobProvider).where(SyncJobProvider.status == CellStatus.awaiting_auth)
        if provider is not None:
            stmt = stmt.where(SyncJobProvider.provider == provider)
        return list(self._session.scalars(stmt))

    def request_cancel(self, *, job_id: str | None = None, batch_id: str | None = None) -> int:
        """Cooperative cancel: flag every non-terminal cell `cancel_requested`.

        The runner polls this via `is_cancel_requested` at stage boundaries;
        this call never touches an already-terminal cell.
        """
        if job_id is None and batch_id is None:
            raise ValueError("request_cancel requires job_id or batch_id")
        # `awaiting_auth` is non-terminal too (task-21): a lane parked waiting
        # for a login must be cancellable, otherwise «Stop» silently did
        # nothing and the cell stayed non-terminal forever. The parked lane
        # re-reads its own cell and honours this flag (`app/jobs/lane.py`).
        non_terminal = (CellStatus.queued, CellStatus.running, CellStatus.awaiting_auth)
        stmt = (
            update(SyncJobProvider)
            .where(SyncJobProvider.status.in_(non_terminal))
            .values(status=CellStatus.cancel_requested)
        )
        stmt = (
            stmt.where(SyncJobProvider.job_id == job_id)
            if job_id
            else stmt.where(SyncJobProvider.batch_id == batch_id)
        )
        result = cast("CursorResult[Any]", self._session.execute(stmt))
        self._session.flush()
        return result.rowcount or 0
