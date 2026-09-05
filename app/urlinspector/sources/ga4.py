"""GA4 `ga4_landing_pages` source for the URL Inspector (task-61 §2.3).

`ga4_landing_pages.dimension_value` stores a bare PATH (`landingPage`
GA4 dimension), never a host — `app.urlinspector.urlkey.ga4_landing_variants`
is the one place that difference is handled, so this module never has to
think about hosts at all.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import AnalyticsDatasetRow, ClientIntegration
from app.schemas.enums import Provider
from app.schemas.url_inspector import Granularity, UrlSeriesDTO, UrlSourceStateDTO
from app.urlinspector.series import PeriodRow, build_series
from app.urlinspector.urlkey import ga4_landing_variants

_DATASET_KEY = "ga4_landing_pages"

# (metric key stored in `metrics_json`, unit)
_METRICS: tuple[tuple[str, str], ...] = (
    ("sessions", "count"),
    ("users", "count"),
    ("conversions", "count"),
    ("views", "count"),
    ("engagement_rate", "ratio"),
)


def _metric_value(row: AnalyticsDatasetRow, field: str) -> Decimal | None:
    """`metrics_json` values are stored as strings (Reporting Ninja's raw
    shape, task-09) — a key entirely ABSENT from the dict is a genuinely
    different thing from a present-but-unparsable value, but both collapse
    to `None` here since neither can be shown as a number (PLAN §12: never
    substitute 0 for either)."""
    if field not in row.metrics_json:
        return None
    raw = row.metrics_json.get(field)
    if raw is None:
        return None
    try:
        return Decimal(str(raw))
    except InvalidOperation:
        return None


def collect_ga4(
    session: Session, *, client_id: str, url: str, granularity: Granularity
) -> tuple[UrlSourceStateDTO, dict[str, UrlSeriesDTO]]:
    """Returns (`reporting_ninja` coverage state, `{metric_key: UrlSeriesDTO}`)."""
    integration = session.scalars(
        select(ClientIntegration).where(
            ClientIntegration.client_id == client_id,
            ClientIntegration.provider == Provider.reporting_ninja,
        )
    ).one_or_none()
    if integration is None or not integration.enabled:
        state = UrlSourceStateDTO(
            source="reporting_ninja",
            state="not_configured",
            detail="Reporting Ninja / GA4 isn't connected for this client.",
        )
        return state, {}

    variants = ga4_landing_variants(url)
    matched = list(
        session.scalars(
            select(AnalyticsDatasetRow).where(
                AnalyticsDatasetRow.client_id == client_id,
                AnalyticsDatasetRow.dataset_key == _DATASET_KEY,
                AnalyticsDatasetRow.dimension_value.in_(variants),
            )
        )
    )
    existence_rows = list(
        session.scalars(
            select(AnalyticsDatasetRow).where(
                AnalyticsDatasetRow.client_id == client_id,
                AnalyticsDatasetRow.dataset_key == _DATASET_KEY,
                AnalyticsDatasetRow.rank == 1,
            )
        )
    )

    if not matched and not existence_rows:
        state = UrlSourceStateDTO(
            source="reporting_ninja", state="no_data", detail="No GA4 data collected yet."
        )
        return state, {}

    if not matched:
        state = UrlSourceStateDTO(
            source="reporting_ninja",
            state="no_data",
            detail="This URL has no GA4 landing-page rows.",
        )
    else:
        state = UrlSourceStateDTO(source="reporting_ninja", state="has_data")

    period_existence = [
        PeriodRow(
            period_start=r.period_start,
            period_end=r.period_end,
            created_at=r.created_at,
            quality=r.quality,
            value=None,
        )
        for r in existence_rows
    ]

    series: dict[str, UrlSeriesDTO] = {}
    for field, unit in _METRICS:
        url_rows = [
            PeriodRow(
                period_start=r.period_start,
                period_end=r.period_end,
                created_at=r.created_at,
                quality=r.quality,
                value=_metric_value(r, field),
            )
            for r in matched
        ]
        series[field] = build_series(
            metric=field,
            unit=unit,
            url_rows=url_rows,
            period_existence=period_existence,
            granularity=granularity,
        )
    return state, series
