"""Snapshot repository (`provider_snapshots`, PLAN §6). Rows are immutable
after commit — only inserts and reads happen here, no update path exists.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import ProviderSnapshot
from app.schemas.enums import Provider, Quality, SourceMode

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
        self, client_id: str, provider: Provider, *, before: datetime | None = None
    ) -> ProviderSnapshot | None:
        stmt = (
            select(ProviderSnapshot)
            .where(
                ProviderSnapshot.client_id == client_id,
                ProviderSnapshot.provider == provider,
                ProviderSnapshot.quality.in_(_SUCCESS_QUALITIES),
            )
            .order_by(ProviderSnapshot.observed_at.desc())
        )
        if before is not None:
            stmt = stmt.where(ProviderSnapshot.observed_at <= before)
        return self._session.scalars(stmt).first()
