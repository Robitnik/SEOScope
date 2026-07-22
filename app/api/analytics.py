"""GSC dimensional analytics endpoints (PLAN §10/§21.4, task-16): per-page and
per-query clicks/impressions/CTR/position for a client, filtered by period.

Read-only: this router never triggers collection. It resolves the requested
period the same way every other endpoint does (`resolve_periods`) and looks
up the exact matching stored period in `gsc_dimension_metrics` — extended
periods (quarter/year/custom) that were never collected at that exact range
come back with an honest empty/`unavailable` answer rather than a fabricated
cross-period aggregate (out of this task's scope; see its Completion report).
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Literal

from fastapi import APIRouter, HTTPException
from sqlalchemy.orm import Session

from app.api import register_router
from app.api.deps import PaginationDep, SessionDep
from app.db.models import Client, DimensionKind, GscDimensionMetric
from app.db.repositories.clients import ClientRepository
from app.db.repositories.dimensions import DimensionRepository
from app.metrics import resolve_periods
from app.schemas.api import DimensionAnalyticsResponseDTO, DimensionRowDTO, ErrorDTO
from app.schemas.enums import ComparisonMode, PeriodKind, Quality

router = APIRouter(prefix="/api/v1", tags=["analytics"])

_NOT_FOUND = ErrorDTO(error_code="client_not_found", message="Client not found").model_dump()
SortKey = Literal["clicks", "impressions", "ctr", "position"]


def _anchor_datetime(anchor: date | None) -> datetime:
    if anchor is None:
        return datetime.now(UTC)
    return datetime.combine(anchor, datetime.min.time().replace(hour=12), tzinfo=UTC)


def _to_row_dto(row: GscDimensionMetric) -> DimensionRowDTO:
    return DimensionRowDTO(
        dimension_value=row.dimension_value,
        clicks=row.clicks,
        impressions=row.impressions,
        ctr=row.ctr,
        position=row.position,
        quality=row.quality,
        rank=row.rank,
    )


async def _get_dimension_analytics(
    client_id: str,
    session: SessionDep,
    page: PaginationDep,
    *,
    dimension: DimensionKind,
    period: PeriodKind,
    anchor: date | None,
    comparison_mode: ComparisonMode,
    custom_start: date | None,
    custom_end: date | None,
    sort_by: SortKey,
) -> DimensionAnalyticsResponseDTO:
    anchor_dt = _anchor_datetime(anchor)

    def _run(
        s: Session,
    ) -> tuple[Client, list[GscDimensionMetric], int, date, date] | None:
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
        rows, total = DimensionRepository(s).for_period(
            client_id,
            dimension,
            periods.current.start,
            periods.current.end,
            sort_by=sort_by,
            limit=page.limit,
            offset=page.offset,
        )
        return client, rows, total, periods.current.start, periods.current.end

    result = await session.run_sync(_run)
    if result is None:
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    _client, rows, total, period_start, period_end = result

    # Truncation is a property of the stored period, not of this response's
    # own page: even a page with zero truncated rows in it must still say so
    # if the period it belongs to was cut (PLAN §12 — never quietly implied).
    truncated = any(r.quality == Quality.truncated for r in rows) if rows else False

    return DimensionAnalyticsResponseDTO(
        client_id=client_id,
        dimension=dimension.value,
        period_start=period_start if total else None,
        period_end=period_end if total else None,
        rows=[_to_row_dto(r) for r in rows],
        total_rows_available=total,
        truncated=truncated,
        limit=page.limit,
        offset=page.offset,
        generated_at=datetime.now(UTC),
    )


@router.get(
    "/clients/{client_id}/analytics/pages",
    response_model=DimensionAnalyticsResponseDTO,
    responses={404: {"model": ErrorDTO}},
)
async def get_page_analytics(
    client_id: str,
    session: SessionDep,
    page: PaginationDep,
    period: PeriodKind = PeriodKind.week,
    anchor: date | None = None,
    comparison_mode: ComparisonMode = ComparisonMode.closed,
    custom_start: date | None = None,
    custom_end: date | None = None,
    sort_by: SortKey = "clicks",
) -> DimensionAnalyticsResponseDTO:
    return await _get_dimension_analytics(
        client_id,
        session,
        page,
        dimension=DimensionKind.page,
        period=period,
        anchor=anchor,
        comparison_mode=comparison_mode,
        custom_start=custom_start,
        custom_end=custom_end,
        sort_by=sort_by,
    )


@router.get(
    "/clients/{client_id}/analytics/queries",
    response_model=DimensionAnalyticsResponseDTO,
    responses={404: {"model": ErrorDTO}},
)
async def get_query_analytics(
    client_id: str,
    session: SessionDep,
    page: PaginationDep,
    period: PeriodKind = PeriodKind.week,
    anchor: date | None = None,
    comparison_mode: ComparisonMode = ComparisonMode.closed,
    custom_start: date | None = None,
    custom_end: date | None = None,
    sort_by: SortKey = "clicks",
) -> DimensionAnalyticsResponseDTO:
    return await _get_dimension_analytics(
        client_id,
        session,
        page,
        dimension=DimensionKind.query,
        period=period,
        anchor=anchor,
        comparison_mode=comparison_mode,
        custom_start=custom_start,
        custom_end=custom_end,
        sort_by=sort_by,
    )


register_router(router)
