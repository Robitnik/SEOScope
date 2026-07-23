"""`ProviderAdapter` for GA4 via Reporting Ninja (task-09; PLAN §2.2/§3.2/§8.2/§8.3).

Maps `seo_audit.dashboard.reporting_ninja.collect_ga4` (AISeoTools, task-08/09) onto the
`rn_*` catalog keys (task-04). `CollectionContext.integration_config` shape for this
provider — both non-secret external identifiers (PLAN §7):

    {"connection_key": "<ninja connection key>", "account_id": "properties/<id>"}

`REPORTING_NINJA_API_KEY` itself never lives here or in the DB: `ReportingNinjaClient`
reads it from AISeoTools' own `.env` (see AISeoTools `seo_audit/clientreport/api_client.py`).

`seo_audit` is only imported lazily, inside method bodies — never at module import time —
so that merely importing/registering this adapter (which `app/integrations/__init__.py`
does unconditionally) never fails in an environment without the local AISeoTools checkout
(e.g. CI; see compose.yaml/docker/entrypoint.sh). Actually calling `collect()`/`preflight()`
without it present degrades to `not_configured`, never a crash.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, cast

from app.integrations.registry import register_adapter
from app.metrics.catalog import CATALOG
from app.schemas.adapter import (
    CollectionContext,
    CollectionResult,
    DateRange,
    MetricValueResult,
    PreflightResult,
    ProgressEvent,
)
from app.schemas.analytics_datasets import (
    AnalyticsDatasetCollectionResult,
    AnalyticsDatasetValueResult,
)
from app.schemas.enums import MetricUnit, PeriodRole, Provider, Quality

# seo_audit.dashboard.reporting_ninja metric_key -> catalog key (task-04). Source-neutral
# names on the left (AISeoTools has no notion of this app's catalog), canonical on the right.
_METRIC_KEY_MAP: dict[str, str] = {
    "users": "rn_users",
    "organic_users": "rn_organic_users",
    "total_conversions": "rn_total_conversions",
    "organic_conversions": "rn_organic_conversions",
}


_GA4_DATASETS: dict[str, dict[str, Any]] = {
    "ga4_daily": {
        "dimension": "date",
        "metrics": {
            "users": "totalUsers",
            "sessions": "sessions",
            "conversions": "keyEvents",
            "engagement_rate": "engagementRate",
        },
        "sort": "date",
        "limit": 1000,
    },
    "ga4_channels": {
        "dimension": "sessionDefaultChannelGroup",
        "metrics": {
            "users": "totalUsers",
            "sessions": "sessions",
            "conversions": "keyEvents",
            "engagement_rate": "engagementRate",
        },
        "sort": "sessions",
        "limit": 100,
    },
    "ga4_landing_pages": {
        "dimension": "landingPage",
        "metrics": {
            "users": "totalUsers",
            "sessions": "sessions",
            "conversions": "keyEvents",
            "engagement_rate": "engagementRate",
            "views": "screenPageViews",
        },
        "sort": "sessions",
        "limit": 500,
    },
    "ga4_events": {
        "dimension": "eventName",
        "metrics": {
            "events": "eventCount",
            "conversions": "keyEvents",
            "users": "totalUsers",
        },
        "sort": "events",
        "limit": 250,
    },
    "ga4_devices": {
        "dimension": "deviceCategory",
        "metrics": {
            "users": "totalUsers",
            "sessions": "sessions",
            "conversions": "keyEvents",
        },
        "sort": "sessions",
        "limit": 50,
    },
    "ga4_countries": {
        "dimension": "country",
        "metrics": {
            "users": "totalUsers",
            "sessions": "sessions",
            "conversions": "keyEvents",
        },
        "sort": "sessions",
        "limit": 250,
    },
}

_GBP_PERFORMANCE_METRICS = {
    "impressions": "business_impressions_total_calc",
    "interactions": "business_profile_interactions_calc",
    "calls": "call_clicks",
    "directions": "business_direction_requests",
    "website_clicks": "website_clicks",
    "bookings": "business_bookings",
}

_GBP_DATASET_KEYS = (
    "gbp_daily",
    "gbp_locations",
    "gbp_search_terms",
    "gbp_reviews",
)


def _decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, ValueError, TypeError):
        return None


def _normalize_dimension(field: str, value: Any) -> str:
    text = str(value or "").strip()
    if field == "date" and len(text) == 8 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:]}"
    return text


class ReportingNinjaAdapter:
    provider = Provider.reporting_ninja

    def preflight(self, context: CollectionContext) -> PreflightResult:
        cfg = context.integration_config
        if not cfg.get("connection_key") or not cfg.get("account_id"):
            return PreflightResult(
                provider=self.provider,
                ok=False,
                reason_code="not_configured",
                message="Reporting Ninja: connection_key/account_id not set for this client.",
            )
        if not os.environ.get("REPORTING_NINJA_API_KEY"):
            return PreflightResult(
                provider=self.provider,
                ok=False,
                reason_code="auth_required",
                message="REPORTING_NINJA_API_KEY not configured.",
            )
        try:
            from seo_audit.clientreport.api_client import ReportingNinjaClient
        except ImportError:
            return PreflightResult(
                provider=self.provider,
                ok=False,
                reason_code="not_configured",
                message="AISeoTools not installed in this environment.",
            )
        ReportingNinjaClient().close()
        return PreflightResult(provider=self.provider, ok=True)

    def collect(
        self,
        context: CollectionContext,
        emit: Callable[[ProgressEvent], None],
        is_cancel_requested: Callable[[], bool],
    ) -> CollectionResult:
        try:
            return self._collect(context, emit)
        except BaseException as exc:  # noqa: BLE001 - never crash the cell (PLAN §9.2)
            return CollectionResult(
                provider=self.provider,
                quality=Quality.unavailable,
                observed_at=datetime.now(UTC),
                metrics=[],
                warnings=[f"unexpected error: {type(exc).__name__}"],
                error_code="adapter_error",
            )

    def _collect(
        self, context: CollectionContext, emit: Callable[[ProgressEvent], None]
    ) -> CollectionResult:
        from seo_audit.clientreport.api_client import ReportingNinjaClient
        from seo_audit.dashboard.models import DateRange as SeoDateRange
        from seo_audit.dashboard.reporting_ninja import collect_ga4

        cfg = context.integration_config
        current = SeoDateRange(start=context.current.start, end=context.current.end)
        previous = SeoDateRange(start=context.previous.start, end=context.previous.end)

        with ReportingNinjaClient() as client:
            result = collect_ga4(
                client,
                ga4_ref={
                    "connection_key": cfg["connection_key"],
                    "account_id": cfg["account_id"],
                },
                current=current,
                previous=previous,
                on_event=_bridge_emit(emit),
            )

        metrics = _map_metrics(result, context.current, context.previous)
        return CollectionResult(
            provider=self.provider,
            quality=Quality(result.quality.value),
            observed_at=result.observed_at,
            metrics=metrics,
            warnings=list(result.warnings),
            error_code=result.error_code,
            provenance=dict(result.provenance),
        )

    def collect_datasets(
        self,
        context: CollectionContext,
        emit: Callable[[ProgressEvent], None],
    ) -> AnalyticsDatasetCollectionResult:
        """Collect GA4 daily and breakdown datasets via Reporting Ninja."""
        try:
            return self._collect_datasets(context, emit)
        except BaseException as exc:  # noqa: BLE001 - optional datasets never crash KPI sync
            return AnalyticsDatasetCollectionResult(
                provider=self.provider,
                quality=Quality.unavailable,
                observed_at=datetime.now(UTC),
                warnings=[f"unexpected dataset error: {type(exc).__name__}"],
                error_code="dataset_adapter_error",
            )

    def _collect_datasets(
        self,
        context: CollectionContext,
        emit: Callable[[ProgressEvent], None],
    ) -> AnalyticsDatasetCollectionResult:
        from seo_audit.clientreport.api_client import (
            APIError,
            RateLimitError,
            ReportingNinjaClient,
        )

        cfg = context.integration_config
        observed_at = datetime.now(UTC)
        warnings: list[str] = []
        values: list[AnalyticsDatasetValueResult] = []
        completed = 0
        truncated_any = False

        def append_dataset(
            *,
            dataset_key: str,
            dimension_key: str,
            role: PeriodRole,
            period: DateRange,
            rows: list[tuple[str, dict[str, Decimal | None]]],
            sort_key: str,
            limit: int,
        ) -> None:
            nonlocal completed, truncated_any
            if sort_key == "date":
                rows.sort(key=lambda item: item[0])
            else:
                rows.sort(
                    key=lambda item: item[1].get(sort_key) or Decimal(0), reverse=True
                )
            capped = rows[:limit]
            truncated = len(rows) > len(capped)
            truncated_any = truncated_any or truncated
            row_quality = Quality.truncated if truncated else Quality.complete
            values.extend(
                AnalyticsDatasetValueResult(
                    dataset_key=dataset_key,
                    dimension_key=dimension_key,
                    dimension_value=dimension_value,
                    period_role=role,
                    period_start=period.start,
                    period_end=period.end,
                    metrics=metrics,
                    quality=row_quality,
                    rank=rank,
                )
                for rank, (dimension_value, metrics) in enumerate(capped, start=1)
            )
            completed += 1

        with ReportingNinjaClient() as client:
            available = {field["field_id"] for field in client.list_fields("ga4")}
            for role, period in (
                (PeriodRole.current, context.current),
                (PeriodRole.previous, context.previous),
            ):
                for dataset_key, definition in _GA4_DATASETS.items():
                    dimension = definition["dimension"]
                    metric_fields = {
                        key: field
                        for key, field in definition["metrics"].items()
                        if field in available
                    }
                    if dimension not in available or not metric_fields:
                        warnings.append(f"{dataset_key}: required GA4 fields unavailable")
                        continue
                    emit(
                        ProgressEvent(
                            provider=self.provider,
                            stage=f"fetch_{role.value}_{dataset_key}",
                            message=f"GA4 {dataset_key} {period.start} → {period.end}",
                        )
                    )
                    try:
                        page = client.query_all(
                            "ga4",
                            cfg["connection_key"],
                            cfg["account_id"],
                            [dimension, *metric_fields.values()],
                            period.start.isoformat(),
                            period.end.isoformat(),
                            page_size=10000,
                            max_rows=50000,
                        )
                    except (APIError, RateLimitError, ValueError) as exc:
                        warnings.append(f"{dataset_key} {role.value} failed: {exc}")
                        continue

                    normalized = []
                    for row in page.get("rows", []):
                        dimension_value = _normalize_dimension(dimension, row.get(dimension))
                        if not dimension_value:
                            continue
                        normalized.append(
                            (
                                dimension_value,
                                {
                                    key: _decimal(row.get(field))
                                    for key, field in metric_fields.items()
                                },
                            )
                        )
                    append_dataset(
                        dataset_key=dataset_key,
                        dimension_key=dimension,
                        role=role,
                        period=period,
                        rows=normalized,
                        sort_key=definition["sort"],
                        limit=definition["limit"],
                    )
                    if page.get("truncated"):
                        truncated_any = True

            gbp_locations = cfg.get("gbp_locations") or []
            if gbp_locations:
                for role, period in (
                    (PeriodRole.current, context.current),
                    (PeriodRole.previous, context.previous),
                ):
                    emit(
                        ProgressEvent(
                            provider=self.provider,
                            stage=f"fetch_{role.value}_gbp",
                            message=(
                                f"Business Profile {role.value}: "
                                f"{len(gbp_locations)} locations"
                            ),
                        )
                    )
                    daily: dict[str, dict[str, Decimal]] = {}
                    location_rows: list[tuple[str, dict[str, Decimal | None]]] = []
                    search_terms: dict[str, Decimal] = {}
                    performance_success = 0
                    search_success = 0
                    for location in gbp_locations:
                        name = str(location.get("name") or location["account_id"])
                        try:
                            performance = client.query_all(
                                "google_business_profile",
                                location["connection_key"],
                                location["account_id"],
                                ["day", *_GBP_PERFORMANCE_METRICS.values()],
                                period.start.isoformat(),
                                period.end.isoformat(),
                                data_view="performance",
                                page_size=1000,
                                max_rows=5000,
                            )
                            performance_success += 1
                            totals = {key: Decimal(0) for key in _GBP_PERFORMANCE_METRICS}
                            for row in performance.get("rows", []):
                                day = _normalize_dimension("date", row.get("day"))
                                if not day:
                                    continue
                                day_metrics = daily.setdefault(
                                    day,
                                    {key: Decimal(0) for key in _GBP_PERFORMANCE_METRICS},
                                )
                                for key, field in _GBP_PERFORMANCE_METRICS.items():
                                    amount = _decimal(row.get(field)) or Decimal(0)
                                    day_metrics[key] += amount
                                    totals[key] += amount
                            location_rows.append(
                                (name, cast("dict[str, Decimal | None]", totals))
                            )
                        except (APIError, RateLimitError, ValueError, KeyError) as exc:
                            warnings.append(f"GBP performance {name} {role.value} failed: {exc}")

                        try:
                            terms_page = client.query_all(
                                "google_business_profile",
                                location["connection_key"],
                                location["account_id"],
                                ["search_terms", "search_impressions"],
                                period.start.isoformat(),
                                period.end.isoformat(),
                                data_view="search_terms",
                                page_size=10000,
                                max_rows=50000,
                            )
                            search_success += 1
                            for row in terms_page.get("rows", []):
                                term = str(row.get("search_terms") or "").strip()
                                if term:
                                    search_terms[term] = search_terms.get(
                                        term, Decimal(0)
                                    ) + (_decimal(row.get("search_impressions")) or Decimal(0))
                        except (APIError, RateLimitError, ValueError, KeyError) as exc:
                            warnings.append(f"GBP search terms {name} {role.value} failed: {exc}")

                    if performance_success:
                        append_dataset(
                            dataset_key="gbp_daily",
                            dimension_key="day",
                            role=role,
                            period=period,
                            rows=[
                                (day, cast("dict[str, Decimal | None]", metrics))
                                for day, metrics in daily.items()
                            ],
                            sort_key="date",
                            limit=1000,
                        )
                        append_dataset(
                            dataset_key="gbp_locations",
                            dimension_key="location",
                            role=role,
                            period=period,
                            rows=location_rows,
                            sort_key="interactions",
                            limit=100,
                        )
                    if search_success:
                        append_dataset(
                            dataset_key="gbp_search_terms",
                            dimension_key="search_terms",
                            role=role,
                            period=period,
                            rows=[
                                (
                                    term,
                                    cast(
                                        "dict[str, Decimal | None]",
                                        {"impressions": amount},
                                    ),
                                )
                                for term, amount in search_terms.items()
                            ],
                            sort_key="impressions",
                            limit=500,
                        )

                reviews: list[tuple[str, dict[str, Decimal | None]]] = []
                emit(
                    ProgressEvent(
                        provider=self.provider,
                        stage="fetch_gbp_reviews",
                        message=(
                            "Business Profile lifetime reviews: "
                            f"{len(gbp_locations)} locations"
                        ),
                    )
                )
                for location in gbp_locations:
                    name = str(location.get("name") or location["account_id"])
                    try:
                        page = client.query_page(
                            "google_business_profile",
                            location["connection_key"],
                            location["account_id"],
                            ["reviews_lifetime", "average_rating_lifetime"],
                            context.current.start.isoformat(),
                            context.current.end.isoformat(),
                            data_view="reviews_lifetime",
                            limit=10,
                        )
                        row = (page.get("rows") or [{}])[0]
                        reviews.append(
                            (
                                name,
                                {
                                    "reviews": _decimal(row.get("reviews_lifetime")),
                                    "average_rating": _decimal(
                                        row.get("average_rating_lifetime")
                                    ),
                                },
                            )
                        )
                    except (APIError, RateLimitError, ValueError, KeyError) as exc:
                        warnings.append(f"GBP reviews {name} failed: {exc}")
                if reviews:
                    append_dataset(
                        dataset_key="gbp_reviews",
                        dimension_key="location",
                        role=PeriodRole.current,
                        period=context.current,
                        rows=reviews,
                        sort_key="reviews",
                        limit=100,
                    )

        expected = len(_GA4_DATASETS) * 2
        if cfg.get("gbp_locations"):
            expected += 7
        if completed == 0:
            quality = Quality.unavailable
        elif completed < expected:
            quality = Quality.partial
        elif truncated_any:
            quality = Quality.truncated
        else:
            quality = Quality.complete
        return AnalyticsDatasetCollectionResult(
            provider=self.provider,
            quality=quality,
            observed_at=observed_at,
            rows=values,
            warnings=warnings,
            provenance={
                "transport": "reporting_ninja",
                "integration": "ga4",
                "datasets": [*list(_GA4_DATASETS), *_GBP_DATASET_KEYS],
            },
        )


def _map_metrics(result: Any, current: DateRange, previous: DateRange) -> list[MetricValueResult]:
    by_key = {(v.metric_key, v.period_role.value): v for v in result.values}
    out: list[MetricValueResult] = []
    for field_key, catalog_key in _METRIC_KEY_MAP.items():
        for role, date_range in ((PeriodRole.current, current), (PeriodRole.previous, previous)):
            found = by_key.get((field_key, role.value))
            if found is not None:
                out.append(
                    MetricValueResult(
                        metric_key=catalog_key,
                        period_role=role,
                        period_start=found.period_start,
                        period_end=found.period_end,
                        value_numeric=found.value_numeric,
                        unit=MetricUnit(found.unit.value),
                        quality=Quality(result.quality.value),
                    )
                )
            else:
                out.append(_unavailable_metric(catalog_key, role, date_range))
    return out


def _unavailable_metric(
    catalog_key: str, role: PeriodRole, date_range: DateRange
) -> MetricValueResult:
    return MetricValueResult(
        metric_key=catalog_key,
        period_role=role,
        period_start=date_range.start,
        period_end=date_range.end,
        value_numeric=None,
        unit=CATALOG[catalog_key].unit,
        quality=Quality.unavailable,
    )


def _bridge_emit(emit: Callable[[ProgressEvent], None]) -> Callable[[Any], None]:
    """Translate a `seo_audit.dashboard.events.ProgressEvent` into this app's own
    `ProgressEvent` and forward it. Best-effort in both directions: a broken callback or an
    unexpected event shape must never interrupt collection."""

    def _on_event(evt: Any) -> None:
        try:
            emit(
                ProgressEvent(
                    provider=Provider(evt.provider.value),
                    stage=evt.stage,
                    completed=evt.completed,
                    total=evt.total,
                    percent=evt.percent,
                    level=evt.level,
                    message=evt.message,
                    details=dict(evt.details),
                )
            )
        except Exception:  # noqa: BLE001 - progress is diagnostic, never load-bearing
            pass

    return _on_event


register_adapter(ReportingNinjaAdapter())
