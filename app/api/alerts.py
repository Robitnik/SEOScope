"""Alerts API (PLAN §21.5, task-17): pages/queries declining or rising
sharply between two adjacent periods, sorted by absolute click impact.

Read-only, same shape as `app.api.analytics`: resolves the requested period
via `resolve_periods`, reads both sides from `gsc_dimension_metrics`
(task-16's `DimensionRepository.for_two_periods`), maps the ORM rows into
`app.metrics.alerts.DimensionRow` and runs the pure `detect_alerts` over
them. Never triggers collection.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Literal

from fastapi import APIRouter, HTTPException
from sqlalchemy.orm import Session

from app.api import register_router
from app.api.deps import SessionDep, SettingsDep
from app.db.models import Client, DimensionKind, GscDimensionMetric
from app.db.repositories.clients import ClientRepository
from app.db.repositories.dimensions import DimensionRepository
from app.metrics.alerts import (
    AlertEntry,
    AlertsResult,
    AlertThresholds,
    DimensionRow,
    detect_alerts,
)
from app.metrics.periods import resolve_periods
from app.schemas.api import AlertEntryDTO, AlertsResponseDTO, ErrorDTO
from app.schemas.enums import ComparisonMode, PeriodKind

router = APIRouter(prefix="/api/v1", tags=["alerts"])

_NOT_FOUND = ErrorDTO(error_code="client_not_found", message="Client not found").model_dump()
DimensionParam = Literal["page", "query"]

_DIMENSION_KIND: dict[DimensionParam, DimensionKind] = {
    "page": DimensionKind.page,
    "query": DimensionKind.query,
}


def _anchor_datetime(anchor: date | None) -> datetime:
    if anchor is None:
        return datetime.now(UTC)
    return datetime.combine(anchor, datetime.min.time().replace(hour=12), tzinfo=UTC)


def _to_dimension_row(row: GscDimensionMetric) -> DimensionRow:
    return DimensionRow(
        dimension_value=row.dimension_value,
        clicks=row.clicks,
        impressions=row.impressions,
        quality=row.quality,
        rank=row.rank,
    )


def _to_entry_dto(entry: AlertEntry) -> AlertEntryDTO:
    return AlertEntryDTO(
        dimension_value=entry.dimension_value,
        clicks_current=entry.clicks_current,
        clicks_previous=entry.clicks_previous,
        delta_abs=entry.delta_abs,
        delta_pct=entry.delta_pct,
    )


@router.get(
    "/clients/{client_id}/alerts",
    response_model=AlertsResponseDTO,
    responses={404: {"model": ErrorDTO}},
)
async def get_alerts(
    client_id: str,
    session: SessionDep,
    settings: SettingsDep,
    period: PeriodKind = PeriodKind.week,
    dimension: DimensionParam = "page",
    anchor: date | None = None,
    comparison_mode: ComparisonMode = ComparisonMode.closed,
    custom_start: date | None = None,
    custom_end: date | None = None,
) -> AlertsResponseDTO:
    anchor_dt = _anchor_datetime(anchor)
    dimension_kind = _DIMENSION_KIND[dimension]

    def _run(
        s: Session,
    ) -> (
        tuple[Client, list[GscDimensionMetric], list[GscDimensionMetric], date, date, date, date]
        | None
    ):
        client = ClientRepository(s).get(client_id)
        if client is None:
            return None
        periods = resolve_periods(
            period,
            anchor_dt,
            tz=client.timezone,
            mode=comparison_mode,
            custom_start=custom_start,
            custom_end=custom_end,
        )
        current_rows, previous_rows = DimensionRepository(s).for_two_periods(
            client_id,
            dimension_kind,
            current=(periods.current.start, periods.current.end),
            previous=(periods.previous.start, periods.previous.end),
        )
        return (
            client,
            current_rows,
            previous_rows,
            periods.current.start,
            periods.current.end,
            periods.previous.start,
            periods.previous.end,
        )

    result = await session.run_sync(_run)
    if result is None:
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    _client, current_rows, previous_rows, cur_start, cur_end, prev_start, prev_end = result

    thresholds = AlertThresholds(
        min_relative_change_pct=settings.alert_min_relative_change_pct,
        min_base_impressions=settings.alert_min_base_impressions,
        min_base_clicks=settings.alert_min_base_clicks,
        top_n_per_group=settings.alert_top_n_per_group,
    )
    alerts: AlertsResult = detect_alerts(
        [_to_dimension_row(r) for r in current_rows],
        [_to_dimension_row(r) for r in previous_rows],
        thresholds,
    )

    return AlertsResponseDTO(
        client_id=client_id,
        dimension=dimension,
        period_current_start=cur_start,
        period_current_end=cur_end,
        period_previous_start=prev_start,
        period_previous_end=prev_end,
        declining=[_to_entry_dto(e) for e in alerts.declining],
        rising=[_to_entry_dto(e) for e in alerts.rising],
        new=[_to_entry_dto(e) for e in alerts.new],
        skipped_reason=alerts.skipped_reason,
        generated_at=datetime.now(UTC),
    )


register_router(router)
