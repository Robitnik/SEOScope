"""GSC dimensional data repository (`gsc_dimension_metrics`, PLAN §21.4,
task-16). Rows are immutable after commit — only inserts, reads and
retention deletes happen here, mirroring `MetricRepository`/`SnapshotRepository`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal, cast

from sqlalchemy import CursorResult, delete, func, select
from sqlalchemy.orm import Session

from app.db.models import DimensionKind, GscDimensionMetric
from app.schemas.enums import Quality

DEFAULT_DIMENSION_RETENTION_DAYS = 14
MIN_DIMENSION_RETENTION_DAYS = 7

_SortKey = Literal["clicks", "impressions", "ctr", "position"]


@dataclass(frozen=True)
class DimensionRowInput:
    """One row to persist — the shape `app.integrations.gsc`'s dimensional
    mapping produces, pre-insert. `rank=None` marks the top-N cut's
    aggregated remainder row (`app.db.models.DIMENSION_OTHER_VALUE`)."""

    dimension_value: str
    clicks: int | None
    impressions: int | None
    ctr: Decimal | None
    position: Decimal | None
    quality: Quality
    rank: int | None


class DimensionRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def bulk_insert(
        self,
        *,
        snapshot_id: str,
        client_id: str,
        dimension: str,
        period_start: date,
        period_end: date,
        rows: list[DimensionRowInput],
    ) -> list[GscDimensionMetric]:
        orm_rows = [
            GscDimensionMetric(
                snapshot_id=snapshot_id,
                client_id=client_id,
                dimension=dimension,
                dimension_value=r.dimension_value,
                period_start=period_start,
                period_end=period_end,
                clicks=r.clicks,
                impressions=r.impressions,
                ctr=r.ctr,
                position=r.position,
                quality=r.quality,
                rank=r.rank,
            )
            for r in rows
        ]
        self._session.add_all(orm_rows)
        self._session.flush()
        return orm_rows

    def for_period(
        self,
        client_id: str,
        dimension: str,
        period_start: date,
        period_end: date,
        *,
        sort_by: _SortKey = "clicks",
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[GscDimensionMetric], int]:
        """Rows of the most recently collected snapshot whose period exactly
        matches `[period_start, period_end]` (PLAN §12 — never averaged
        across mismatched ranges), sorted by `sort_by` descending.

        Returns `(page, total_row_count)`; `total_row_count` is the full set
        size for this exact period (before `limit`/`offset`), so a caller can
        tell a paginated page apart from a top-N-truncated one.
        """
        latest_snapshot_id = self._session.scalar(
            select(GscDimensionMetric.snapshot_id)
            .where(
                GscDimensionMetric.client_id == client_id,
                GscDimensionMetric.dimension == dimension,
                GscDimensionMetric.period_start == period_start,
                GscDimensionMetric.period_end == period_end,
            )
            .order_by(GscDimensionMetric.created_at.desc())
            .limit(1)
        )
        if latest_snapshot_id is None:
            return [], 0

        sort_column = getattr(GscDimensionMetric, sort_by)
        base = select(GscDimensionMetric).where(
            GscDimensionMetric.snapshot_id == latest_snapshot_id,
            GscDimensionMetric.dimension == dimension,
        )
        total = self._session.scalar(select(func.count()).select_from(base.subquery())) or 0
        stmt = (
            base.order_by(sort_column.desc().nulls_last(), GscDimensionMetric.dimension_value)
            .limit(limit)
            .offset(offset)
        )
        rows = list(self._session.scalars(stmt))
        return rows, total

    def top_pages(
        self,
        client_id: str,
        period_start: date,
        period_end: date,
        *,
        sort_by: _SortKey = "clicks",
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[GscDimensionMetric], int]:
        """`for_period` scoped to the `page` dimension — named per this
        task's documented `Provides` contract (task-17/18 consumers)."""
        return self.for_period(
            client_id,
            DimensionKind.page,
            period_start,
            period_end,
            sort_by=sort_by,
            limit=limit,
            offset=offset,
        )

    def top_queries(
        self,
        client_id: str,
        period_start: date,
        period_end: date,
        *,
        sort_by: _SortKey = "clicks",
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[GscDimensionMetric], int]:
        """`for_period` scoped to the `query` dimension — see `top_pages`."""
        return self.for_period(
            client_id,
            DimensionKind.query,
            period_start,
            period_end,
            sort_by=sort_by,
            limit=limit,
            offset=offset,
        )

    def for_two_periods(
        self,
        client_id: str,
        dimension: str,
        *,
        current: tuple[date, date],
        previous: tuple[date, date],
    ) -> tuple[list[GscDimensionMetric], list[GscDimensionMetric]]:
        """Stored rows for both sides of a comparison — the shape task-17's
        alerts engine needs (per-page clicks/impressions across two adjacent
        periods). Each side independently resolves to its own latest
        matching snapshot, same as `for_period`; a side with no exact stored
        match comes back empty rather than an invented aggregate.
        """
        current_rows, _ = self.for_period(
            client_id, dimension, current[0], current[1], limit=10_000
        )
        previous_rows, _ = self.for_period(
            client_id, dimension, previous[0], previous[1], limit=10_000
        )
        return current_rows, previous_rows

    def purge_older_than(self, days: int = DEFAULT_DIMENSION_RETENTION_DAYS) -> int:
        """Delete rows whose `period_end` is older than `days` ago.

        Dimensional data grows far faster than `metric_values` (PLAN §21.4
        risk: SQLite blows up without a short, separate retention), so the
        default here is intentionally shorter than `event_retention_days`.
        Wiring this into `scripts/retention_cleanup.py` (task-13's owned
        file) alongside `EventRepository.purge_older_than` is a one-line
        follow-up — see this task's Completion report.
        """
        if days < MIN_DIMENSION_RETENTION_DAYS:
            raise ValueError(
                f"refusing days={days}; below the safe minimum of "
                f"{MIN_DIMENSION_RETENTION_DAYS} days"
            )
        cutoff = datetime.now(UTC).date() - timedelta(days=days)
        result = cast(
            "CursorResult[Any]",
            self._session.execute(
                delete(GscDimensionMetric).where(GscDimensionMetric.period_end < cutoff)
            ),
        )
        self._session.flush()
        return result.rowcount or 0
