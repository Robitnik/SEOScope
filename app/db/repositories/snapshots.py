"""Snapshot repository (`provider_snapshots`, PLAN §6). Rows are immutable
after commit — only inserts and reads happen here, no update path exists.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import exists, select
from sqlalchemy.orm import Session

from app.db.models import MetricValue, ProviderSnapshot
from app.schemas.enums import PeriodRole, Provider, Quality, SourceMode

# Any quality except `unavailable` counts as a successful collection attempt
# worth comparing future periods against (PLAN §12).
_SUCCESS_QUALITIES = (Quality.complete, Quality.partial, Quality.truncated, Quality.stale)


class SnapshotRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def create_immutable(
        self,
        *,
        job_id: str,
        client_id: str,
        provider: Provider,
        source_mode: SourceMode,
        observed_at: datetime,
        quality: Quality,
        source_version: str | None = None,
        data_through: date | None = None,
        warnings: list[str] | None = None,
        provenance: dict[str, Any] | None = None,
        raw_artifact_id: str | None = None,
    ) -> ProviderSnapshot:
        snapshot = ProviderSnapshot(
            job_id=job_id,
            client_id=client_id,
            provider=provider,
            source_mode=source_mode,
            source_version=source_version,
            observed_at=observed_at,
            data_through=data_through,
            quality=quality,
            warnings_json=warnings or [],
            provenance_json=provenance or {},
            raw_artifact_id=raw_artifact_id,
        )
        self._session.add(snapshot)
        self._session.flush()
        return snapshot

    def latest_success(
        self,
        client_id: str,
        provider: Provider,
        *,
        before: datetime | None = None,
        period_start: date | None = None,
        period_end: date | None = None,
        source_mode: SourceMode | None = None,
    ) -> ProviderSnapshot | None:
        stmt = (
            select(ProviderSnapshot)
            .where(
                ProviderSnapshot.client_id == client_id,
                ProviderSnapshot.provider == provider,
                ProviderSnapshot.quality.in_(_SUCCESS_QUALITIES),
            )
            .order_by(ProviderSnapshot.observed_at.desc(), ProviderSnapshot.created_at.desc())
        )
        if before is not None:
            stmt = stmt.where(ProviderSnapshot.observed_at <= before)
        if source_mode is not None:
            stmt = stmt.where(ProviderSnapshot.source_mode == source_mode)
        if period_start is not None or period_end is not None:
            if period_start is None or period_end is None:
                raise ValueError("period_start and period_end must be provided together")
            stmt = stmt.where(
                exists().where(
                    MetricValue.snapshot_id == ProviderSnapshot.id,
                    MetricValue.period_role == PeriodRole.current,
                    MetricValue.period_start == period_start,
                    MetricValue.period_end == period_end,
                )
            )
        return self._session.scalars(stmt).first()

    def latest_for_jobs(self, job_ids: list[str]) -> dict[tuple[str, Provider], ProviderSnapshot]:
        """Newest snapshot for every requested job/provider cell, including
        ``unavailable`` attempts so the progress API can report their quality.
        """
        if not job_ids:
            return {}
        stmt = (
            select(ProviderSnapshot)
            .where(ProviderSnapshot.job_id.in_(job_ids))
            .order_by(ProviderSnapshot.observed_at.desc(), ProviderSnapshot.created_at.desc())
        )
        result: dict[tuple[str, Provider], ProviderSnapshot] = {}
        for snapshot in self._session.scalars(stmt):
            result.setdefault((snapshot.job_id, snapshot.provider), snapshot)
        return result
