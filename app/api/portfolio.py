"""Portfolio Trends API (task-28): one line per client across the whole
portfolio for a single daily metric (GSC clicks/impressions, GA4
sessions/users), bucketed into day/week/month buckets.

Read-only, same shape as `app.api.analytics`/`app.api.alerts`: never
triggers collection, reads whatever day-level history
`AnalyticsDatasetRepository.daily_series` already has stored (task-09's
`gsc_daily`/`ga4_daily`). The heavy lifting — bucketing, `None` vs real `0`,
partial/provisional flags — lives in the pure `app.metrics.portfolio`.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy.orm import Session

from app.api import register_router
from app.api.deps import SessionDep
from app.db.models import AnalyticsDatasetRow, Client, ClientIntegration
from app.db.repositories.analytics_datasets import AnalyticsDatasetRepository
from app.db.repositories.clients import ClientRepository
from app.metrics.portfolio import (
    MAX_WINDOW_DAYS,
    PORTFOLIO_SERIES,
    Bucket,
    aggregate_client_series,
    build_buckets,
)
from app.schemas.adapter import DateRange
from app.schemas.api import (
    ErrorDTO,
    PortfolioBucketDTO,
    PortfolioClientSeriesDTO,
    PortfolioExcludedClientDTO,
    PortfolioMetricDTO,
    PortfolioPointDTO,
    PortfolioSeriesDTO,
)

router = APIRouter(prefix="/api/v1", tags=["portfolio"])

_METRIC_NOT_FOUND_TEMPLATE = "Unknown portfolio metric '{metric}'"


class _ClientsNotFoundError(Exception):
    """Mirrors `app.api.jobs.ClientsNotFoundError` — same shape, own module
    since importing across API routers is not this codebase's convention."""

    def __init__(self, client_ids: list[str]) -> None:
        self.client_ids = client_ids


def _error(status_code: int, error_code: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail=ErrorDTO(error_code=error_code, message=message).model_dump(),
    )


@router.get(
    "/analytics/portfolio-series",
    response_model=PortfolioSeriesDTO,
    responses={404: {"model": ErrorDTO}, 422: {"model": ErrorDTO}},
)
async def get_portfolio_series(
    session: SessionDep,
    metric: str,
    bucket: Bucket = "week",
    days: Annotated[int, Query(ge=1, le=MAX_WINDOW_DAYS)] = 90,
    start: date | None = None,
    end: date | None = None,
    client_ids: Annotated[list[str] | None, Query()] = None,
) -> PortfolioSeriesDTO:
    spec = PORTFOLIO_SERIES.get(metric)
    if spec is None:
        raise _error(404, "metric_not_found", _METRIC_NOT_FOUND_TEMPLATE.format(metric=metric))

    today = datetime.now(UTC).date()
    if (start is None) != (end is None):
        raise _error(
            422, "invalid_range", "start and end must be provided together, or neither"
        )
    if start is not None and end is not None:
        if end < start:
            raise _error(422, "invalid_range", f"end {end} precedes start {start}")
        window_start, window_end = start, end
    else:
        window_end = today - timedelta(days=1)
        window_start = window_end - timedelta(days=days - 1)

    window_days = (window_end - window_start).days + 1
    if window_days > MAX_WINDOW_DAYS:
        raise _error(
            422,
            "range_too_wide",
            f"requested window ({window_days} days) exceeds {MAX_WINDOW_DAYS}",
        )

    def _run(
        s: Session,
    ) -> tuple[
        list[Client],
        dict[str, ClientIntegration | None],
        dict[str, dict[date, AnalyticsDatasetRow]],
    ]:
        clients_repo = ClientRepository(s)
        if client_ids is not None:
            clients: list[Client] = []
            missing: list[str] = []
            for client_id in client_ids:
                found = clients_repo.get(client_id)
                if found is None:
                    missing.append(client_id)
                else:
                    clients.append(found)
            if missing:
                raise _ClientsNotFoundError(missing)
        else:
            # Дефолт портфеля — активні клієнти (task-34): лінія клієнта, що
            # припинив оплату, більше не збирається, і тримати її в загальному
            # зрізі означає малювати спад, якого насправді не було. Явні
            # `client_ids` (зокрема паузований) віддаються як і раніше.
            clients = clients_repo.list_collectable()

        integrations = {
            client.id: clients_repo.get_integration(client.id, spec.provider)
            for client in clients
        }
        daily = AnalyticsDatasetRepository(s).daily_series(
            [client.id for client in clients],
            spec.provider,
            spec.dataset_key,
            window_start,
            window_end,
        )
        return clients, integrations, daily

    try:
        clients, integrations, daily = await session.run_sync(_run)
    except _ClientsNotFoundError as exc:
        raise _error(
            404,
            "client_not_found",
            f"one or more client_ids do not exist: {', '.join(exc.client_ids)}",
        ) from exc

    buckets = build_buckets(window_start, window_end, bucket, today=today, lag_days=spec.lag_days)

    series: list[PortfolioClientSeriesDTO] = []
    excluded: list[PortfolioExcludedClientDTO] = []
    for client in clients:
        integration = integrations.get(client.id)
        if integration is None or not integration.enabled:
            excluded.append(
                PortfolioExcludedClientDTO(
                    client_id=client.id,
                    name=client.name,
                    domain=client.domain,
                    state="not_configured",
                    reason=f"{spec.provider.value} integration is not set up for this client.",
                )
            )
            continue

        client_rows = daily.get(client.id, {})
        client_daily: dict[date, Decimal] = {}
        for day, row in client_rows.items():
            raw = row.metrics_json.get(spec.field)
            if raw is not None:
                client_daily[day] = Decimal(str(raw))

        aggregate = aggregate_client_series(client_daily, buckets)
        if aggregate.days_with_data == 0:
            excluded.append(
                PortfolioExcludedClientDTO(
                    client_id=client.id,
                    name=client.name,
                    domain=client.domain,
                    state="no_data",
                    reason="No daily history collected yet for this range.",
                )
            )
            continue

        series.append(
            PortfolioClientSeriesDTO(
                client_id=client.id,
                name=client.name,
                domain=client.domain,
                points=[
                    PortfolioPointDTO(
                        value=point.value,
                        days_with_data=point.days_with_data,
                        quality=point.quality,
                    )
                    for point in aggregate.points
                ],
                first_day=aggregate.first_day,
                last_day=aggregate.last_day,
                days_with_data=aggregate.days_with_data,
                days_expected=aggregate.days_expected,
            )
        )

    series.sort(key=lambda item: item.name)
    excluded.sort(key=lambda item: item.name)

    return PortfolioSeriesDTO(
        metric=PortfolioMetricDTO(
            key=spec.key,
            label=spec.label,
            provider=spec.provider,
            unit=spec.unit,
            better_direction=spec.better_direction,
            dataset_key=spec.dataset_key,
            field=spec.field,
            lag_days=spec.lag_days,
        ),
        bucket=bucket,
        range=DateRange(start=window_start, end=window_end),
        buckets=[
            PortfolioBucketDTO(
                start=b.start, end=b.end, days_expected=b.days_expected, provisional=b.provisional
            )
            for b in buckets
        ],
        series=series,
        excluded=excluded,
        generated_at=datetime.now(UTC),
    )


register_router(router)
