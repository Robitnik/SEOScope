"""Metric value repository (`metric_values`, PLAN §6). Rows are immutable
after commit, same as `provider_snapshots`.

Query methods here are generic building blocks — matching a metric's exact
comparison semantics (range vs snapshot, which previous period, freshness
gating) is business logic owned by task-04/task-06, not this repository.
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from app.db.models import MetricValue, ProviderSnapshot
from app.schemas.adapter import MetricValueResult
from app.schemas.enums import PeriodRole, Provider, SourceMode


class MetricRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def bulk_insert(
        self,
        *,
        snapshot_id: str,
        client_id: str,
        provider: Provider,
        values: list[MetricValueResult],
    ) -> list[MetricValue]:
        rows = [
            MetricValue(
                snapshot_id=snapshot_id,
                client_id=client_id,
                provider=provider,
                metric_key=v.metric_key,
                period_role=v.period_role,
                period_start=v.period_start,
                period_end=v.period_end,
                value_numeric=v.value_numeric,
                value_text=v.value_text,
                unit=v.unit,
                quality=v.quality,
                is_derived=v.is_derived,
            )
            for v in values
        ]
        self._session.add_all(rows)
        self._session.flush()
        return rows

    def for_dashboard(
        self, client_ids: list[str], period_start: date, period_end: date
    ) -> list[MetricValue]:
        """Snapshot-role values for these clients, plus range values whose
        period falls inside `[period_start, period_end]`.
        """
        stmt = select(MetricValue).where(
            MetricValue.client_id.in_(client_ids),
            or_(
                and_(MetricValue.period_start.is_(None), MetricValue.period_end.is_(None)),
                and_(
                    MetricValue.period_start >= period_start,
                    MetricValue.period_end <= period_end,
                ),
            ),
        )
        return list(self._session.scalars(stmt))

    def history(
        self,
        client_id: str,
        metric_key: str,
        n: int = 12,
        *,
        source_mode: SourceMode | None = None,
    ) -> list[MetricValue]:
        stmt = (
            select(MetricValue)
            .where(
                MetricValue.client_id == client_id,
                MetricValue.metric_key == metric_key,
                MetricValue.period_role.in_([PeriodRole.current, PeriodRole.snapshot]),
            )
            .order_by(MetricValue.created_at.desc())
            .limit(n)
        )
        if source_mode is not None:
            stmt = stmt.join(
                ProviderSnapshot, ProviderSnapshot.id == MetricValue.snapshot_id
            ).where(ProviderSnapshot.source_mode == source_mode)
        return list(self._session.scalars(stmt))

    # --- Added by task-06 (REST API): the dashboard needs every metric value
    # tied to one specific `ProviderSnapshot` (both `current`/`previous` role
    # rows for a range metric, or the single `snapshot`-role row for a
    # snapshot metric), which `for_dashboard`'s period-bounds filter cannot
    # express since snapshot-role rows always carry `period_start/end = None`.
    # See this task's Completion report, "Contract changes", for the rationale.

    def for_snapshot(self, snapshot_id: str) -> list[MetricValue]:
        stmt = select(MetricValue).where(MetricValue.snapshot_id == snapshot_id)
        return list(self._session.scalars(stmt))
