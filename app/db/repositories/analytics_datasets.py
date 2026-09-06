"""Persistence for normalized provider analytical datasets."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, aliased

from app.db.models import AnalyticsDatasetRow
from app.schemas.enums import Provider, Quality

DatasetScalar = Any


@dataclass(frozen=True)
class AnalyticsDatasetRowInput:
    dataset_key: str
    dimension_key: str
    dimension_value: str
    metrics: dict[str, DatasetScalar]
    quality: Quality
    rank: int | None


class AnalyticsDatasetRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def bulk_insert(
        self,
        *,
        snapshot_id: str,
        client_id: str,
        provider: Provider,
        period_start: date,
        period_end: date,
        rows: list[AnalyticsDatasetRowInput],
    ) -> list[AnalyticsDatasetRow]:
        records = [
            AnalyticsDatasetRow(
                snapshot_id=snapshot_id,
                client_id=client_id,
                provider=provider,
                dataset_key=row.dataset_key,
                dimension_key=row.dimension_key,
                dimension_value=row.dimension_value,
                period_start=period_start,
                period_end=period_end,
                metrics_json={
                    key: str(value) if isinstance(value, Decimal) else value
                    for key, value in row.metrics.items()
                },
                quality=row.quality,
                rank=row.rank,
            )
            for row in rows
        ]
        self._session.add_all(records)
        self._session.flush()
        return records

    def for_period(
        self,
        client_id: str,
        dataset_key: str,
        period_start: date,
        period_end: date,
    ) -> list[AnalyticsDatasetRow]:
        latest_snapshot_id = self._session.scalar(
            select(AnalyticsDatasetRow.snapshot_id)
            .where(
                AnalyticsDatasetRow.client_id == client_id,
                AnalyticsDatasetRow.dataset_key == dataset_key,
                AnalyticsDatasetRow.period_start == period_start,
                AnalyticsDatasetRow.period_end == period_end,
            )
            .order_by(AnalyticsDatasetRow.created_at.desc())
            .limit(1)
        )
        if latest_snapshot_id is None:
            return []
        return list(
            self._session.scalars(
                select(AnalyticsDatasetRow)
                .where(
                    AnalyticsDatasetRow.snapshot_id == latest_snapshot_id,
                    AnalyticsDatasetRow.dataset_key == dataset_key,
                )
                .order_by(AnalyticsDatasetRow.rank.asc().nulls_last())
            )
        )

    def latest(
        self,
        client_id: str,
        provider: Provider,
        dataset_key: str,
    ) -> list[AnalyticsDatasetRow]:
        """Rows from the newest provider snapshot, independent of report period.

        Snapshot datasets such as Ahrefs Site Audit describe the latest crawl,
        not the week/month selected in the client UI.
        """
        latest_snapshot_id = self._session.scalar(
            select(AnalyticsDatasetRow.snapshot_id)
            .where(
                AnalyticsDatasetRow.client_id == client_id,
                AnalyticsDatasetRow.provider == provider,
                AnalyticsDatasetRow.dataset_key == dataset_key,
            )
            .order_by(AnalyticsDatasetRow.created_at.desc())
            .limit(1)
        )
        if latest_snapshot_id is None:
            return []
        return list(
            self._session.scalars(
                select(AnalyticsDatasetRow)
                .where(
                    AnalyticsDatasetRow.snapshot_id == latest_snapshot_id,
                    AnalyticsDatasetRow.dataset_key == dataset_key,
                )
                .order_by(AnalyticsDatasetRow.rank.asc().nulls_last())
            )
        )

    def latest_rows_by_dimension(
        self,
        client_id: str,
        provider: Provider,
        dataset_key: str,
        *,
        since: date | None = None,
        until: date | None = None,
    ) -> dict[str, AnalyticsDatasetRow]:
        """Newest row per ``dimension_value`` across ALL snapshots for this dataset.

        Unlike ``latest`` (one newest snapshot), this spans snapshots so a dataset
        whose rows accrue over many small runs — task-25's per-finding recheck,
        where each recheck job writes only the ``(slug, url)`` pairs it touched —
        resolves to the most recent status of every dimension, not just those in
        the single latest run.

        task-68 §2: dedup moved from Python (`setdefault` over a full,
        unbounded scan) into SQL, via `ROW_NUMBER() OVER (PARTITION BY
        dimension_value ORDER BY created_at DESC)`, keeping row 1 per
        partition. Live-verified byte-for-byte identical to the old
        Python dedup across all 15 production clients x both call sites
        (`gsc_daily`/`gbp_daily`) before this replaced it — same rows,
        same `.id`, same `.created_at`, same `metrics_json`.

        `since`/`until` are optional and, like `daily_series` (task-28),
        compare against `dimension_value` as ISO date text — only
        meaningful for a date-keyed dataset (`gsc_daily`/`gbp_daily`;
        `dimension_value` is `YYYY-MM-DD`), which is what both current
        callers are. Live-measured need: `gsc_daily` for the busiest
        client loads 1333 rows unbounded, 512 unique — but even the 30
        rows a caller then keeps for its trend window come from just
        150-165 rows once actually windowed (dashboard's own `since`),
        of which only ~28 are unique per day. Both narrowings (window,
        then dedup) matter; neither alone gets there.
        """
        conditions = [
            AnalyticsDatasetRow.client_id == client_id,
            AnalyticsDatasetRow.provider == provider,
            AnalyticsDatasetRow.dataset_key == dataset_key,
        ]
        if since is not None:
            conditions.append(AnalyticsDatasetRow.dimension_value >= since.isoformat())
        if until is not None:
            conditions.append(AnalyticsDatasetRow.dimension_value <= until.isoformat())

        row_number = func.row_number().over(
            partition_by=AnalyticsDatasetRow.dimension_value,
            order_by=AnalyticsDatasetRow.created_at.desc(),
        ).label("rn")
        subquery = select(AnalyticsDatasetRow, row_number).where(*conditions).subquery()
        newest = aliased(AnalyticsDatasetRow, subquery)
        rows = self._session.scalars(select(newest).where(subquery.c.rn == 1))
        return {row.dimension_value: row for row in rows}

    # --- Added by task-28 (Portfolio Trends): the trends page needs one
    # day-level dataset (`gsc_daily`/`ga4_daily`) read across MANY clients at
    # once and clipped to an arbitrary day window — `for_period` requires an
    # exact `(period_start, period_end)` match against one stored period, and
    # `latest_rows_by_dimension` is scoped to a single client. This mirrors
    # both: "newest `created_at` wins per dimension_value" (same rule as
    # `latest_rows_by_dimension`), but multi-client and window-bounded.
    # `dimension_value` is a `YYYY-MM-DD` string (task-09's daily datasets),
    # so it sorts and compares correctly as text against ISO date bounds.
    # See this task's Completion report, "Contract changes", for the rationale.

    def daily_series(
        self,
        client_ids: Sequence[str],
        provider: Provider,
        dataset_key: str,
        since: date,
        until: date,
    ) -> dict[str, dict[date, AnalyticsDatasetRow]]:
        if not client_ids:
            return {}
        rows = self._session.scalars(
            select(AnalyticsDatasetRow)
            .where(
                AnalyticsDatasetRow.client_id.in_(client_ids),
                AnalyticsDatasetRow.provider == provider,
                AnalyticsDatasetRow.dataset_key == dataset_key,
                AnalyticsDatasetRow.dimension_value >= since.isoformat(),
                AnalyticsDatasetRow.dimension_value <= until.isoformat(),
            )
            .order_by(AnalyticsDatasetRow.created_at.desc())
        )
        out: dict[str, dict[date, AnalyticsDatasetRow]] = {}
        for row in rows:
            try:
                day = date.fromisoformat(row.dimension_value)
            except ValueError:
                continue
            out.setdefault(row.client_id, {}).setdefault(day, row)
        return out
