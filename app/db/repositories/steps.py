"""Step repository (`sync_job_steps`, PLAN §6): sub-stage progress within a cell."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.base import utcnow
from app.db.models import SyncJobStep
from app.schemas.enums import Provider, StageStatus


class StepRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    # --- Added by task-06 (REST API): `JobDTO.steps` needs every step row for
    # a job; no existing method here lists rather than upserts/reads-one.
    # See this task's Completion report, "Contract changes", for the rationale.

    def list_for_job(self, job_id: str) -> list[SyncJobStep]:
        stmt = select(SyncJobStep).where(SyncJobStep.job_id == job_id)
        return list(self._session.scalars(stmt))

    def upsert_step(
        self,
        job_id: str,
        provider: Provider,
        stage: str,
        attempt: int = 1,
        *,
        status: StageStatus = StageStatus.pending,
        progress_percent: float | None = None,
        units_spent: int | None = None,
        units_estimated: int | None = None,
    ) -> SyncJobStep:
        step = self._session.scalars(
            select(SyncJobStep).where(
                SyncJobStep.job_id == job_id,
                SyncJobStep.provider == provider,
                SyncJobStep.stage == stage,
                SyncJobStep.attempt == attempt,
            )
        ).one_or_none()
        if step is None:
            step = SyncJobStep(
                job_id=job_id,
                provider=provider,
                stage=stage,
                attempt=attempt,
                status=status,
                progress_percent=progress_percent,
                units_spent=units_spent,
                units_estimated=units_estimated,
            )
            self._session.add(step)
        else:
            step.status = status
            step.progress_percent = progress_percent
            if units_spent is not None:
                step.units_spent = units_spent
            if units_estimated is not None:
                step.units_estimated = units_estimated
        if status == StageStatus.running and step.started_at is None:
            step.started_at = utcnow()
        self._session.flush()
        return step

    def set_step_status(
        self,
        job_id: str,
        provider: Provider,
        stage: str,
        attempt: int,
        status: StageStatus,
        *,
        warning_summary: str | None = None,
        error_summary: str | None = None,
    ) -> SyncJobStep:
        step = self._session.scalars(
            select(SyncJobStep).where(
                SyncJobStep.job_id == job_id,
                SyncJobStep.provider == provider,
                SyncJobStep.stage == stage,
                SyncJobStep.attempt == attempt,
            )
        ).one_or_none()
        if step is None:
            raise LookupError(f"step not found: {job_id}/{provider}/{stage}/{attempt}")
        step.status = status
        step.warning_summary = warning_summary
        step.error_summary = error_summary
        if status in (StageStatus.succeeded, StageStatus.failed, StageStatus.skipped):
            step.finished_at = utcnow()
        self._session.flush()
        return step
