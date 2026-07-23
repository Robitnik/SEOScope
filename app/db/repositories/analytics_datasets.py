"""Persistence for normalized provider analytical datasets."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

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
    ) -> dict[str, AnalyticsDatasetRow]:
        """Newest row per ``dimension_value`` across ALL snapshots for this dataset.

        Unlike ``latest`` (one newest snapshot), this spans snapshots so a dataset
        whose rows accrue over many small runs — task-25's per-finding recheck,
        where each recheck job writes only the ``(slug, url)`` pairs it touched —
        resolves to the most recent status of every dimension, not just those in
        the single latest run.
        """
        rows = self._session.scalars(
            select(AnalyticsDatasetRow)
            .where(
                AnalyticsDatasetRow.client_id == client_id,
                AnalyticsDatasetRow.provider == provider,
                AnalyticsDatasetRow.dataset_key == dataset_key,
            )
            .order_by(AnalyticsDatasetRow.created_at.desc())
        )
        out: dict[str, AnalyticsDatasetRow] = {}
        for row in rows:
            out.setdefault(row.dimension_value, row)
        return out
