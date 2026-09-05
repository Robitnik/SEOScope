"""GSC page-dimension source for the URL Inspector (task-61 §2.3).

Reads `gsc_dimension_metrics` directly — same style `app/api/analytics.py`
and `app/db/repositories/tech_audits.py` already use for read-only
cross-cutting queries, rather than adding a new method to
`app/db/repositories/dimensions.py` (owned by task-16/17, read-only here).
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import ClientIntegration, DimensionKind, GscDimensionMetric
from app.schemas.enums import Provider
from app.schemas.url_inspector import Granularity, UrlSeriesDTO, UrlSourceStateDTO
from app.urlinspector.series import PeriodRow, build_series
from app.urlinspector.urlkey import lookup_variants

_METRICS: tuple[tuple[str, str], ...] = (
    ("clicks", "count"),
    ("impressions", "count"),
    ("ctr", "ratio"),
    ("position", "position"),
)


def _metric_value(row: GscDimensionMetric, field: str) -> Decimal | None:
    raw = getattr(row, field)
    if raw is None:
        return None
    return raw if isinstance(raw, Decimal) else Decimal(raw)


def collect_gsc(
    session: Session, *, client_id: str, url: str, granularity: Granularity
) -> tuple[UrlSourceStateDTO, dict[str, UrlSeriesDTO]]:
    """Returns (`gsc` coverage state, `{metric_key: UrlSeriesDTO}`)."""
    integration = session.scalars(
        select(ClientIntegration).where(
            ClientIntegration.client_id == client_id,
            ClientIntegration.provider == Provider.gsc,
        )
    ).one_or_none()
    if integration is None or not integration.enabled:
        state = UrlSourceStateDTO(
            source="gsc", state="not_configured", detail="GSC isn't connected for this client."
        )
        return state, {}

    variants = lookup_variants(url)
    matched = list(
        session.scalars(
            select(GscDimensionMetric).where(
                GscDimensionMetric.client_id == client_id,
                GscDimensionMetric.dimension == DimensionKind.page,
                GscDimensionMetric.dimension_value.in_(variants),
            )
        )
    )
    # `rank == 1` picks one representative row per (period, snapshot) instead
    # of every URL in the period — bounded by periods x duplicate snapshots,
    # not by portfolio size (task-61 §0.3/§2.3).
    existence_rows = list(
        session.scalars(
            select(GscDimensionMetric).where(
                GscDimensionMetric.client_id == client_id,
                GscDimensionMetric.dimension == DimensionKind.page,
                GscDimensionMetric.rank == 1,
            )
        )
    )

    if not matched and not existence_rows:
        state = UrlSourceStateDTO(
            source="gsc", state="no_data", detail="No Search Console data collected yet."
        )
        return state, {}

    if not matched:
        state = UrlSourceStateDTO(
            source="gsc", state="no_data", detail="This URL has no Search Console rows."
        )
    else:
        state = UrlSourceStateDTO(source="gsc", state="has_data")

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
