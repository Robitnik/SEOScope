"""Cell repository: atomic claim/lease/status for one `(client, provider)` cell
(`sync_job_providers`, PLAN §9.1/§9.2).

SQLite has no `SELECT ... FOR UPDATE SKIP LOCKED`; atomicity instead comes
from a conditional `UPDATE ... WHERE id = :id AND status = 'queued'` whose
row count proves whether *this* call won the claim — safe under SQLite's
single-writer model (PLAN §5.1) even when called from several lanes in the
same process.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, cast

from sqlalchemy import CursorResult, exists, select, update
from sqlalchemy.orm import Session, aliased

from app.db.base import utcnow
from app.db.models import SyncJobProvider
from app.schemas.enums import CellStatus, Provider


class CellRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def claim_next_cell(
        self, provider: Provider, *, lease_owner: str, lease_ttl_seconds: int = 300
    ) -> SyncJobProvider | None:
        now = utcnow()
        stmt = (
            select(SyncJobProvider)
            .where(
                SyncJobProvider.provider == provider, SyncJobProvider.status == CellStatus.queued
            )
            .order_by(SyncJobProvider.created_at)
        )
        for cell in self._session.scalars(stmt):
            if cell.profile_key is not None and not self.acquire_profile_lock(cell.profile_key):
                continue

            claim_conditions = [
                SyncJobProvider.id == cell.id,
                SyncJobProvider.status == CellStatus.queued,
            ]
            if cell.profile_key is not None:
                holder = aliased(SyncJobProvider)
                claim_conditions.append(
                    ~exists().where(
                        holder.profile_key == cell.profile_key,
                        holder.status == CellStatus.running,
                    )
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
                    )
                ),
            )
            if result.rowcount == 1:
                self._session.flush()
                self._session.refresh(cell)
                return cell
        return None

    def acquire_profile_lock(self, profile_key: str) -> bool:
        """True if no cell is currently `running` while holding this profile key."""
        stmt = select(SyncJobProvider.id).where(
            SyncJobProvider.profile_key == profile_key,
            SyncJobProvider.status == CellStatus.running,
        )
        return self._session.scalars(stmt).first() is None

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

    def awaiting_auth_cells(self) -> list[SyncJobProvider]:
        """`awaiting_auth` cells left over from a previous process — no lane
        from a previous instance can still be polling them after a restart,
        so they are always safe to requeue unconditionally (mirrors
        `stale_cancel_requested_cells`).
        """
        stmt = select(SyncJobProvider).where(SyncJobProvider.status == CellStatus.awaiting_auth)
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
