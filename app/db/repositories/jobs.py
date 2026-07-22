"""Job repository: decomposes a sync request into `(client, provider)` cells
and aggregates their status back into a job/batch-level `JobStatus` (PLAN §9.1/§9.2).
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.base import new_uuid, utcnow
from app.db.models import ClientIntegration, SyncJob, SyncJobProvider
from app.schemas.enums import CellStatus, ComparisonMode, JobStatus, PeriodKind, Provider

# `JobRepository.list` (below) shadows the builtin `list` for annotations
# written later in this class body; methods after it that return a list
# must spell their return type via this alias instead of `list[...]`.
_SyncJobs = list[SyncJob]

_JOB_TERMINAL = {
    JobStatus.succeeded,
    JobStatus.partial,
    JobStatus.failed,
    JobStatus.cancelled,
}

# Cell statuses that mean "an attempt happened" for the purposes of aggregate
# status (PLAN §9.2): not_configured/skipped are excluded on purpose — a run
# with only those is neither succeeded nor failed by itself.
_ATTEMPTED = {
    CellStatus.succeeded,
    CellStatus.partial,
    CellStatus.failed,
    CellStatus.failed_interrupted,
    CellStatus.cancelled,
}
_TERMINAL = _ATTEMPTED | {CellStatus.not_configured, CellStatus.skipped}


class JobRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def create_run(
        self,
        *,
        client_ids: list[str],
        providers: list[Provider],
        period_kind: PeriodKind,
        comparison_mode: ComparisonMode,
        anchor_date: date,
        mode: str = "collect_missing",
        idempotency_key: str | None = None,
        batch_id: str | None = None,
    ) -> list[SyncJob]:
        """Create one `SyncJob` per client plus a `queued` cell per provider.

        Preflight (deciding `not_configured`/freshness-skip) is the job
        engine's job (task-05), not this repository's — cells are always
        created `queued` here.
        """
        if batch_id is None and len(client_ids) > 1:
            batch_id = new_uuid()

        jobs: list[SyncJob] = []
        for client_id in client_ids:
            job_idempotency_key = f"{idempotency_key}:{client_id}" if idempotency_key else None
            if job_idempotency_key is not None:
                existing = self._session.scalars(
                    select(SyncJob).where(SyncJob.idempotency_key == job_idempotency_key)
                ).one_or_none()
                if existing is not None:
                    jobs.append(existing)
                    continue

            job = SyncJob(
                batch_id=batch_id,
                client_id=client_id,
                period_kind=period_kind,
                comparison_mode=comparison_mode,
                anchor_date=anchor_date,
                requested_providers_json=[p.value for p in providers],
                mode=mode,
                idempotency_key=job_idempotency_key,
            )
            self._session.add(job)
            self._session.flush()

            for provider in providers:
                self._session.add(
                    SyncJobProvider(
                        job_id=job.id,
                        batch_id=batch_id,
                        client_id=client_id,
                        provider=provider,
                        profile_key=self._profile_key(client_id, provider),
                    )
                )
            jobs.append(job)

        self._session.flush()
        return jobs

    def _profile_key(self, client_id: str, provider: Provider) -> str | None:
        """Physical browser-profile lock key, or ``None`` for API-only integrations.

        ``profile_store`` lets integrations declare that two logical profiles
        resolve to one directory (for example GSC pinned to Semrush).  The
        older ``profile_name`` key remains supported for existing rows.
        """
        integration = self._session.scalars(
            select(ClientIntegration).where(
                ClientIntegration.client_id == client_id, ClientIntegration.provider == provider
            )
        ).one_or_none()
        if integration is None:
            return None
        config = integration.config_json or {}
        profile_name = config.get("profile_store") or config.get("profile_name") or config.get(
            "profile"
        )
        return f"browser:{profile_name}" if profile_name else None

    def get(self, job_id: str) -> SyncJob | None:
        return self._session.get(SyncJob, job_id)

    def list(
        self, *, status: JobStatus | None = None, client_id: str | None = None
    ) -> list[SyncJob]:
        stmt = select(SyncJob)
        if status is not None:
            stmt = stmt.where(SyncJob.status == status)
        if client_id is not None:
            stmt = stmt.where(SyncJob.client_id == client_id)
        stmt = stmt.order_by(SyncJob.created_at.desc())
        return list(self._session.scalars(stmt))

    def aggregate_status(
        self, *, job_id: str | None = None, batch_id: str | None = None
    ) -> JobStatus:
        """Aggregate cell statuses into one `JobStatus` (PLAN §9.2).

        `not_configured`/`skipped` cells never count as failures: `succeeded`
        if every *attempted* cell succeeded, `partial` if attempts are mixed,
        `failed` only if every attempted cell failed, `running`/`queued` while
        any cell is still in flight.
        """
        if job_id is None and batch_id is None:
            raise ValueError("aggregate_status requires job_id or batch_id")

        stmt = select(SyncJobProvider.status)
        stmt = stmt.where(SyncJobProvider.job_id == job_id) if job_id else stmt.where(
            SyncJobProvider.batch_id == batch_id
        )
        statuses = list(self._session.scalars(stmt))
        if not statuses:
            return JobStatus.queued

        if any(s not in _TERMINAL for s in statuses):
            if any(s == CellStatus.running for s in statuses):
                return JobStatus.running
            return JobStatus.queued

        attempted = [s for s in statuses if s in _ATTEMPTED]
        if not attempted:
            # Every cell was not_configured/skipped: nothing was attempted.
            return JobStatus.succeeded

        if all(s == CellStatus.cancelled for s in attempted):
            # A deliberately stopped run is `cancelled`, never `failed` — the
            # "zero succeeded" branch below would otherwise report an operator
            # pressing «Stop» as a collection failure.
            return JobStatus.cancelled

        succeeded = sum(1 for s in attempted if s in (CellStatus.succeeded, CellStatus.partial))
        if succeeded == len(attempted):
            return JobStatus.succeeded
        if succeeded == 0:
            return JobStatus.failed
        return JobStatus.partial

    # --- Added by task-05 (job engine): the dispatcher is the only writer
    # active during collection, so it is what keeps `sync_jobs.status` (and
    # `list(status=...)`) truthful — `aggregate_status` alone only computes,
    # never persists. See this task's Completion report, "Contract changes".

    def sync_status_from_cells(self, job_id: str) -> SyncJob:
        """Recompute `aggregate_status` for `job_id` and persist it onto the
        `SyncJob` row, along with a cell-count-based `progress_percent`.
        Call after every cell status transition belonging to this job.
        """
        job = self._session.get(SyncJob, job_id)
        if job is None:
            raise LookupError(f"job not found: {job_id}")

        cells = list(
            self._session.scalars(select(SyncJobProvider).where(SyncJobProvider.job_id == job_id))
        )
        if not cells:
            return job

        status = self.aggregate_status(job_id=job_id)
        if status != job.status:
            job.status = status
            if status == JobStatus.running and job.started_at is None:
                job.started_at = utcnow()
            elif status in _JOB_TERMINAL and job.finished_at is None:
                job.finished_at = utcnow()

        terminal = sum(1 for c in cells if c.status in _TERMINAL)
        job.progress_percent = round(100.0 * terminal / len(cells), 2)
        running_stages = {c.current_stage for c in cells if c.status == CellStatus.running}
        if len(running_stages) == 1:
            job.current_stage = next(iter(running_stages))
        elif not running_stages:
            job.current_stage = None

        self._session.flush()
        return job

    # Added by task-06 (REST API): `GET /sync-jobs/{id}` needs every job in a
    # batch to build `BatchProgressDTO`; no existing method filters by
    # `batch_id` alone. See this task's Completion report, "Contract
    # changes", for the rationale.
    def list_for_batch(self, batch_id: str) -> _SyncJobs:
        stmt = select(SyncJob).where(SyncJob.batch_id == batch_id).order_by(SyncJob.created_at)
        return list(self._session.scalars(stmt))

    # --- Added by task-07 (importer): every `ProviderSnapshot.job_id` is a
    # mandatory FK to `sync_jobs`, but a historical backfill is not a live
    # dispatcher run — there is no request to decompose into queued cells.
    # This creates an already-terminal `SyncJob` plus its one matching
    # `SyncJobProvider` cell (mirroring `create_run`'s job+cells shape) so
    # `CellRepository.latest_for_client_provider` — what the dashboard reads
    # for "last successful" per PLAN §12 — sees backfilled history the same
    # way it would see a live success, instead of a job with zero cells that
    # no other code path here has needed to handle. See this task's
    # Completion report, "Contract changes", for the rationale.
    def create_imported_run(
        self,
        *,
        client_id: str,
        provider: Provider,
        observed_at: datetime,
        period_kind: PeriodKind = PeriodKind.month,
        comparison_mode: ComparisonMode = ComparisonMode.closed,
    ) -> tuple[SyncJob, SyncJobProvider]:
        job = SyncJob(
            client_id=client_id,
            period_kind=period_kind,
            comparison_mode=comparison_mode,
            anchor_date=observed_at.date(),
            requested_providers_json=[provider.value],
            mode="imported_backfill",
            status=JobStatus.succeeded,
            progress_percent=100.0,
            started_at=observed_at,
            finished_at=observed_at,
        )
        self._session.add(job)
        self._session.flush()

        cell = SyncJobProvider(
            job_id=job.id,
            client_id=client_id,
            provider=provider,
            status=CellStatus.succeeded,
            progress_percent=100.0,
            started_at=observed_at,
            finished_at=observed_at,
            last_success_at=observed_at,
        )
        self._session.add(cell)
        self._session.flush()
        return job, cell

    def request_cancel(
        self, *, job_id: str | None = None, batch_id: str | None = None
    ) -> _SyncJobs:
        """Mark every job in scope `cancel_requested`; pair with
        `CellRepository.request_cancel` to flag the underlying cells the
        runner actually polls (`app.jobs.control.cancel_run`).
        """
        if job_id is None and batch_id is None:
            raise ValueError("request_cancel requires job_id or batch_id")
        stmt = select(SyncJob)
        stmt = (
            stmt.where(SyncJob.id == job_id)
            if job_id
            else stmt.where(SyncJob.batch_id == batch_id)
        )
        jobs = list(self._session.scalars(stmt))
        for job in jobs:
            job.status = JobStatus.cancel_requested
        self._session.flush()
        return jobs
