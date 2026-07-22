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

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

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
from app.schemas.enums import MetricUnit, PeriodRole, Provider, Quality

# seo_audit.dashboard.reporting_ninja metric_key -> catalog key (task-04). Source-neutral
# names on the left (AISeoTools has no notion of this app's catalog), canonical on the right.
_METRIC_KEY_MAP: dict[str, str] = {
    "users": "rn_users",
    "organic_users": "rn_organic_users",
    "total_conversions": "rn_total_conversions",
    "organic_conversions": "rn_organic_conversions",
}


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
        from seo_audit.dashboard.models import DateRange as SeoDateRange
        from seo_audit.dashboard.reporting_ninja import collect_ga4

        cfg = context.integration_config
        current = SeoDateRange(start=context.current.start, end=context.current.end)
        previous = SeoDateRange(start=context.previous.start, end=context.previous.end)

        client = ReportingNinjaClient()
        result = collect_ga4(
            client,
            ga4_ref={"connection_key": cfg["connection_key"], "account_id": cfg["account_id"]},
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
