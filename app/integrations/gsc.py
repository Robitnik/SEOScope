"""`ProviderAdapter` for GSC (task-09; PLAN §2.2/§3.2/§8.2/§8.3).

Combines TWO independent AISeoTools sources into one `CollectionResult`:

- `seo_audit.dashboard.gsc.collect_gsc_aggregate` (Reporting Ninja API, the PLAN §8.3
  default source) for `gsc_clicks`/`gsc_impressions`/`gsc_avg_position`.
- `seo_audit.dashboard.gsc.collect_gsc_direct` remains an explicit
  `source_mode=browser` fallback for legacy/manual runs.

`gsc_ctr` is deliberately never emitted here: `app.metrics.normalize.normalize_result`
(task-04) derives it downstream from `gsc_clicks`/`gsc_impressions`, only when both are
`complete` for the same period (PLAN §12) — duplicating that math here would just be a
second implementation to keep in sync.

The two sources fail independently, so quality is tracked per metric (not one blanket
aggregate) — a Ninja outage doesn't have to take `gsc_avg_position` down with it, and a
browser/session problem doesn't have to take clicks/impressions down with it.

`CollectionContext.integration_config` shape for this provider — all non-secret external
identifiers (PLAN §7):

    {
      "connection_key": "<ninja connection key>",  # required
      "account_id": "sc-domain:example.com",        # required (Ninja account ref)
      "property": "sc-domain:example.com",          # optional — enables direct avg_position
      "profile": "gsc",                             # optional, default "gsc"
      "account": 0,                                 # optional Google authuser override
    }

Only `connection_key`/`account_id` gate `preflight()` — Ninja aggregate is the default
source and stands on its own; a missing `property` just leaves `gsc_avg_position`
`unavailable`, not a whole-cell failure.

`seo_audit` is only imported lazily, inside method bodies — never at module import time —
so merely importing/registering this adapter (which `app/integrations/__init__.py` does
unconditionally) never fails in an environment without the local AISeoTools checkout (e.g.
CI; see compose.yaml/docker/entrypoint.sh).
"""

from __future__ import annotations

import tempfile
from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from app.db.models import DIMENSION_OTHER_VALUE
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
from app.schemas.enums import MetricUnit, PeriodRole, Provider, Quality, SourceMode

_AGGREGATE_METRIC_KEY_MAP: dict[str, str] = {
    "clicks": "gsc_clicks",
    "impressions": "gsc_impressions",
    "avg_position": "gsc_avg_position",
}
_DIRECT_METRIC_KEY = "gsc_avg_position"


# --- Added by task-16 (GSC dimensional data, PLAN §21.4) --------------------
#
# `collect_dimensions` stays an optional extension to `ProviderAdapter` (PLAN §7)
# because it returns a row-shaped contract rather than flat metrics.  The worker
# feature-detects and persists it for GSC cells; other adapters remain unchanged.


class DimensionValueResult(BaseModel):
    """One normalized page/query row, pre-persistence — the "dimension row"
    the task-16 plan describes the adapter as producing. `rank=None` marks
    the top-N cut's aggregated remainder row (`DIMENSION_OTHER_VALUE`)."""

    model_config = ConfigDict(extra="forbid")

    dimension: Literal["page", "query"]
    dimension_value: str
    period_role: PeriodRole
    period_start: date
    period_end: date
    clicks: int | None = None
    impressions: int | None = None
    ctr: Decimal | None = None
    position: Decimal | None = None
    quality: Quality
    rank: int | None = None


class DimensionCollectionResult(BaseModel):
    """Result of one `collect_dimensions()` call — mirrors `CollectionResult`'s
    never-raises contract (partial/unavailable instead of an exception)."""

    model_config = ConfigDict(extra="forbid")

    provider: Provider
    quality: Quality
    observed_at: datetime
    data_through: date | None = None
    rows: list[DimensionValueResult] = []
    warnings: list[str] = []
    error_code: str | None = None
    provenance: dict[str, Any] = {}


class GscAdapter:
    provider = Provider.gsc

    def preflight(self, context: CollectionContext) -> PreflightResult:
        cfg = context.integration_config
        if not cfg.get("connection_key") or not cfg.get("account_id"):
            return PreflightResult(
                provider=self.provider,
                ok=False,
                reason_code="not_configured",
                message="GSC: connection_key/account_id not set for this client.",
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
        try:
            ReportingNinjaClient().close()
        except ValueError:
            return PreflightResult(
                provider=self.provider,
                ok=False,
                reason_code="auth_required",
                message="REPORTING_NINJA_API_KEY not configured.",
            )
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
        from seo_audit.dashboard.gsc import collect_gsc_aggregate, collect_gsc_direct
        from seo_audit.dashboard.models import DateRange as SeoDateRange

        cfg = context.integration_config
        current = SeoDateRange(start=context.current.start, end=context.current.end)
        previous = SeoDateRange(start=context.previous.start, end=context.previous.end)
        on_event = _bridge_emit(emit)

        client = ReportingNinjaClient()
        aggregate = collect_gsc_aggregate(
            client,
            gsc_ref={"connection_key": cfg["connection_key"], "account_id": cfg["account_id"]},
            current=current,
            previous=previous,
            on_event=on_event,
        )

        metrics = _map_aggregate(aggregate, context.current, context.previous)
        warnings = list(aggregate.warnings)
        data_through = aggregate.data_through
        provenance: dict[str, Any] = {"aggregate": dict(aggregate.provenance)}

        prop = cfg.get("property")
        if context.source_mode == SourceMode.browser and prop:
            with tempfile.TemporaryDirectory(prefix="gsc-direct-") as tmp:
                direct = collect_gsc_direct(
                    prop,
                    current=current,
                    previous=previous,
                    out_dir=Path(tmp),
                    profile=cfg.get("profile", "gsc"),
                    account=cfg.get("account"),
                    # No launch flags — attach-only consumer; see compose.yaml's volume comment.
                    on_event=on_event,
                )
            # Explicit browser mode replaces the API's average-position values,
            # while clicks/impressions remain the stable Reporting Ninja totals.
            metrics = [m for m in metrics if m.metric_key != _DIRECT_METRIC_KEY]
            metrics.extend(_map_direct(direct, context.current, context.previous))
            warnings.extend(direct.warnings)
            provenance["direct"] = dict(direct.provenance)
            if direct.data_through is not None:
                data_through = (
                    direct.data_through
                    if data_through is None
                    else min(data_through, direct.data_through)
                )
        return CollectionResult(
            provider=self.provider,
            quality=_overall_quality(m.quality for m in metrics),
            observed_at=datetime.now(UTC),
            data_through=data_through,
            metrics=metrics,
            warnings=warnings,
            provenance=provenance,
        )

    def collect_dimensions(
        self,
        context: CollectionContext,
        emit: Callable[[ProgressEvent], None],
        *,
        top_n: int = 1000,
    ) -> DimensionCollectionResult:
        """Per-page/per-query clicks/impressions/CTR/position for two periods.

        Auto/API mode uses Reporting Ninja's cursor-paginated Search Console
        endpoint. Explicit browser mode keeps the original Performance-export
        fallback for diagnostics and regression compatibility.

        Never raises, same invariant as `collect()`: any failure becomes
        `quality=unavailable` plus a stable `error_code`.
        """
        try:
            return self._collect_dimensions(context, emit, top_n=top_n)
        except BaseException as exc:  # noqa: BLE001 - never crash the caller (PLAN §9.2)
            return DimensionCollectionResult(
                provider=self.provider,
                quality=Quality.unavailable,
                observed_at=datetime.now(UTC),
                warnings=[f"unexpected error: {type(exc).__name__}"],
                error_code="adapter_error",
            )

    def collect_datasets(
        self,
        context: CollectionContext,
        emit: Callable[[ProgressEvent], None],
    ) -> AnalyticsDatasetCollectionResult:
        """Collect the real daily GSC series used by the client-page chart."""
        try:
            return self._collect_datasets(context, emit)
        except BaseException as exc:  # noqa: BLE001 - optional chart data never loses KPIs
            return AnalyticsDatasetCollectionResult(
                provider=self.provider,
                quality=Quality.unavailable,
                observed_at=datetime.now(UTC),
                warnings=[f"unexpected GSC daily-series error: {type(exc).__name__}"],
                error_code="dataset_adapter_error",
            )

    def _collect_datasets(
        self,
        context: CollectionContext,
        emit: Callable[[ProgressEvent], None],
    ) -> AnalyticsDatasetCollectionResult:
        from seo_audit.clientreport.api_client import APIError, RateLimitError, ReportingNinjaClient
        from seo_audit.clientreport.gsc_fetcher import discover_gsc_fields

        cfg = context.integration_config
        observed_at = datetime.now(UTC)
        warnings: list[str] = []
        values: list[AnalyticsDatasetValueResult] = []
        completed = 0
        truncated_any = False

        with ReportingNinjaClient() as client:
            fields = discover_gsc_fields(client)
            query_fields = ["date", fields["clicks"], fields["impressions"]]
            for optional in ("ctr", "avg_position"):
                if fields.get(optional):
                    query_fields.append(fields[optional])
            for role, date_range in (
                (PeriodRole.current, context.current),
                (PeriodRole.previous, context.previous),
            ):
                emit(
                    ProgressEvent(
                        provider=self.provider,
                        stage=f"fetch_{role.value}_daily",
                        message=(
                            f"GSC daily trend {role.value}: "
                            f"{date_range.start} → {date_range.end}"
                        ),
                    )
                )
                try:
                    result = client.query_all(
                        "google_search_console",
                        cfg["connection_key"],
                        cfg["account_id"],
                        query_fields,
                        date_range.start.isoformat(),
                        date_range.end.isoformat(),
                        page_size=2000,
                        max_rows=2000,
                        settings={"search_type": "web"},
                        bypass_cache=context.force_refresh,
                    )
                except (APIError, RateLimitError, ValueError, KeyError) as exc:
                    warnings.append(f"GSC daily trend failed for {role.value}: {exc}")
                    continue

                rows = sorted(result.get("rows", []), key=lambda row: str(row.get("date") or ""))
                truncated = bool(result.get("truncated"))
                truncated_any = truncated_any or truncated
                row_quality = Quality.truncated if truncated else Quality.complete
                for rank, row in enumerate(rows, start=1):
                    day = str(row.get("date") or "").strip()
                    if not day:
                        continue
                    values.append(
                        AnalyticsDatasetValueResult(
                            dataset_key="gsc_daily",
                            dimension_key="date",
                            dimension_value=day,
                            period_role=role,
                            period_start=date_range.start,
                            period_end=date_range.end,
                            metrics={
                                "clicks": _dataset_decimal(row.get(fields["clicks"])),
                                "impressions": _dataset_decimal(
                                    row.get(fields["impressions"])
                                ),
                                "ctr": _dataset_decimal(row.get(fields.get("ctr", ""))),
                                "position": _dataset_decimal(
                                    row.get(fields.get("avg_position", ""))
                                ),
                            },
                            quality=row_quality,
                            rank=rank,
                        )
                    )
                completed += 1

        if completed == 0:
            quality = Quality.unavailable
        elif completed < 2:
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
                "integration": "google_search_console",
                "datasets": ["gsc_daily"],
            },
        )

    def _collect_dimensions(
        self, context: CollectionContext, emit: Callable[[ProgressEvent], None], *, top_n: int
    ) -> DimensionCollectionResult:
        from seo_audit.clientreport.api_client import ReportingNinjaClient
        from seo_audit.dashboard.gsc import collect_gsc_dimensions, collect_gsc_dimensions_api
        from seo_audit.dashboard.models import DateRange as SeoDateRange

        cfg = context.integration_config
        if context.source_mode != SourceMode.browser and (
            not cfg.get("connection_key") or not cfg.get("account_id")
        ):
            return DimensionCollectionResult(
                provider=self.provider,
                quality=Quality.unavailable,
                observed_at=datetime.now(UTC),
                warnings=[
                    "GSC: connection_key/account_id not configured for dimensional data."
                ],
                error_code="not_configured",
            )

        current = SeoDateRange(start=context.current.start, end=context.current.end)
        previous = SeoDateRange(start=context.previous.start, end=context.previous.end)
        on_event = _bridge_emit(emit)

        if context.source_mode == SourceMode.browser:
            prop = cfg.get("property")
            if not prop:
                return DimensionCollectionResult(
                    provider=self.provider,
                    quality=Quality.unavailable,
                    observed_at=datetime.now(UTC),
                    warnings=["GSC: property is required for explicit browser mode."],
                    error_code="not_configured",
                )
            with tempfile.TemporaryDirectory(prefix="gsc-dimensions-") as tmp:
                result = collect_gsc_dimensions(
                    prop,
                    current=current,
                    previous=previous,
                    out_dir=Path(tmp),
                    top_n=top_n,
                    profile=cfg.get("profile", "gsc"),
                    account=cfg.get("account"),
                    on_event=on_event,
                )
        else:
            with ReportingNinjaClient() as client:
                result = collect_gsc_dimensions_api(
                    client,
                    gsc_ref={
                        "connection_key": cfg["connection_key"],
                        "account_id": cfg["account_id"],
                    },
                    current=current,
                    previous=previous,
                    top_n=top_n,
                    on_event=on_event,
                )

        rows = [
            *_map_dimension_periods("page", result.pages),
            *_map_dimension_periods("query", result.queries),
        ]
        return DimensionCollectionResult(
            provider=self.provider,
            quality=Quality(result.quality.value),
            observed_at=result.observed_at,
            data_through=result.data_through,
            rows=rows,
            warnings=list(result.warnings),
            error_code=result.error_code,
            provenance=dict(result.provenance),
        )


def _map_dimension_periods(
    dimension: Literal["page", "query"], periods: list[Any]
) -> list[DimensionValueResult]:
    out: list[DimensionValueResult] = []
    for period in periods:
        row_quality = Quality.truncated if period.truncated else Quality.complete
        for idx, row in enumerate(period.rows, start=1):
            is_other = row.dimension_value == DIMENSION_OTHER_VALUE
            out.append(
                DimensionValueResult(
                    dimension=dimension,
                    dimension_value=row.dimension_value,
                    period_role=PeriodRole(period.period_role.value),
                    period_start=period.period_start,
                    period_end=period.period_end,
                    clicks=row.clicks,
                    impressions=row.impressions,
                    ctr=row.ctr,
                    position=row.position,
                    quality=row_quality,
                    rank=None if is_other else idx,
                )
            )
    return out


def _dataset_decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value).replace(",", "").strip())
    except (ValueError, ArithmeticError):
        return None


def _map_aggregate(
    aggregate: Any, current: DateRange, previous: DateRange
) -> list[MetricValueResult]:
    by_key = {(v.metric_key, v.period_role.value): v for v in aggregate.values}
    out: list[MetricValueResult] = []
    for field_key, catalog_key in _AGGREGATE_METRIC_KEY_MAP.items():
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
                        quality=Quality(aggregate.quality.value),
                    )
                )
            else:
                out.append(_unavailable_metric(catalog_key, role, date_range))
    return out


def _map_direct(direct: Any, current: DateRange, previous: DateRange) -> list[MetricValueResult]:
    by_role = {v.period_role.value: v for v in direct.values}
    out: list[MetricValueResult] = []
    for role, date_range in ((PeriodRole.current, current), (PeriodRole.previous, previous)):
        found = by_role.get(role.value)
        if found is not None:
            out.append(
                MetricValueResult(
                    metric_key=_DIRECT_METRIC_KEY,
                    period_role=role,
                    period_start=found.period_start,
                    period_end=found.period_end,
                    value_numeric=found.value_numeric,
                    unit=MetricUnit(found.unit.value),
                    quality=Quality(direct.quality.value),
                )
            )
        else:
            out.append(_unavailable_metric(_DIRECT_METRIC_KEY, role, date_range))
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


def _overall_quality(qualities: Any) -> Quality:
    seen = set(qualities)
    if not seen:
        return Quality.unavailable
    if seen == {Quality.complete}:
        return Quality.complete
    if seen == {Quality.unavailable}:
        return Quality.unavailable
    return Quality.partial


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


register_adapter(GscAdapter())
