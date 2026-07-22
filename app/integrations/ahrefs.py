"""Ahrefs `ProviderAdapter` — lightweight Site Audit snapshot (PLAN §2.2/§3.2).

Maps the AISeoTools dashboard wrapper (`seo_audit.dashboard.ahrefs`) onto the
app's `CollectionResult` contract. The wrapper is source-neutral and knows
nothing about `CollectionContext`/business quality rules; this module owns
the project-specific decisions PLAN §12 requires:

- a metric with no value is `Quality.unavailable`, never a fabricated `0`;
- an Ahrefs crawl older than the requested period's start is `Quality.stale`
  — the crawl date (`provenance`/`data_through`) is a different axis from the
  collection date (`observed_at`), and must never be conflated with it;
- `auto` source mode tries the API first and falls back to a CSV export only
  when the API attempt produced nothing usable (no project / units
  exhausted), each fallback step recorded in `provenance`.

Ahrefs Site Audit has no automated browser scraper in AISeoTools today
(unlike Semrush/GSC/Reporting Ninja) — `SourceMode.browser` here means
reading a manually-exported CSV directory, not driving a live session.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from app.integrations.registry import register_adapter
from app.jobs.fake_adapter import CancelledByAdapter
from app.schemas.adapter import (
    CollectionContext,
    CollectionResult,
    MetricValueResult,
    PreflightResult,
    ProgressEvent,
)
from app.schemas.enums import MetricUnit, PeriodRole, Provider, Quality, SourceMode

if TYPE_CHECKING:
    # Annotation-only. `seo_audit` (AISeoTools) is an OPTIONAL runtime dependency —
    # absent on CI/deploy (PLAN §8/§14) — so it MUST NOT be imported at module load,
    # or `import app.main` fails wherever AISeoTools is not installed. Runtime symbols
    # are imported lazily inside `collect()`; these are for the type-checker only.
    from seo_audit.dashboard.events import ProgressEvent as DashboardProgressEvent
    from seo_audit.dashboard.models import DashboardResult

AHREFS_API_KEY_ENV = "AHREFS_API_KEY"

# Two flat-cost Site Audit endpoints (project lookup + issue list), no
# per-issue affected-URL fetch — see `collect_site_audit`'s own docstring for
# why the 4 dashboard KPIs never need that unbounded loop.
_UNITS_ESTIMATE = 100

# Severity order for aggregating per-metric quality into one `CollectionResult.quality`
# and for picking the better of two fallback attempts. Higher = worse.
_QUALITY_SEVERITY: dict[Quality, int] = {
    Quality.complete: 0,
    Quality.stale: 1,
    Quality.partial: 2,
    Quality.truncated: 3,
    Quality.unavailable: 4,
}


def _dash_quality_rank(value: str) -> int:
    return ["complete", "stale", "partial", "truncated", "unavailable"].index(value)


def _bridge_emit(emit: Callable[[ProgressEvent], None]) -> Callable[[DashboardProgressEvent], None]:
    """Adapt the AISeoTools dashboard `ProgressEvent` into the app's own type
    (field-for-field identical by convention, but a distinct Pydantic class)."""

    def _on_event(event: DashboardProgressEvent) -> None:
        emit(
            ProgressEvent(
                provider=Provider.ahrefs,
                stage=event.stage,
                completed=event.completed,
                total=event.total,
                percent=event.percent,
                level=event.level,
                message=event.message,
                details=event.details,
            )
        )

    return _on_event


def collect_site_audit(
    domain: str,
    *,
    client: str,
    market: str,
    csv_dir: object,
    on_event: Callable[[DashboardProgressEvent], None],
) -> DashboardResult:
    """Module-level lazy seam over the AISeoTools collector.

    Kept as a thin wrapper (not a top-level `from seo_audit… import`) because
    `seo_audit` is an OPTIONAL runtime dependency that MUST NOT be imported at
    module load — it is absent on CI/deploy (PLAN §8/§14), where `import app.main`
    must still succeed. Tests monkeypatch this name (`adapter_module.collect_site_audit`).
    """
    from seo_audit.dashboard.ahrefs import collect_site_audit as _impl

    return _impl(domain, client=client, market=market, csv_dir=csv_dir, on_event=on_event)


class AhrefsAdapter:
    """`ProviderAdapter` for `Provider.ahrefs` (PLAN §7)."""

    provider = Provider.ahrefs

    def preflight(self, context: CollectionContext) -> PreflightResult:
        domain = context.integration_config.get("domain")
        if not domain:
            return PreflightResult(
                provider=self.provider,
                ok=False,
                reason_code="not_configured",
                message="Ahrefs integration_config is missing 'domain'",
            )

        csv_dir = context.integration_config.get("csv_dir")
        csv_available = bool(csv_dir) and Path(str(csv_dir)).is_dir()

        if context.source_mode == SourceMode.pdf:
            return PreflightResult(
                provider=self.provider,
                ok=False,
                reason_code="not_configured",
                message="Ahrefs does not support source_mode=pdf",
            )

        if context.source_mode == SourceMode.browser:
            if not csv_available:
                return PreflightResult(
                    provider=self.provider,
                    ok=False,
                    reason_code="not_configured",
                    message="Ahrefs has no live browser scraper; source_mode=browser "
                    "requires integration_config['csv_dir'] to point at an existing "
                    "directory of manually-exported CSVs",
                )
            return PreflightResult(provider=self.provider, ok=True, units_estimated=0)

        # api or auto
        api_key_present = bool(os.environ.get(AHREFS_API_KEY_ENV))
        if api_key_present:
            return PreflightResult(provider=self.provider, ok=True, units_estimated=_UNITS_ESTIMATE)
        if context.source_mode == SourceMode.auto and csv_available:
            return PreflightResult(provider=self.provider, ok=True, units_estimated=0)
        return PreflightResult(
            provider=self.provider,
            ok=False,
            reason_code="auth_required",
            message=f"{AHREFS_API_KEY_ENV} is not set and no CSV fallback is configured",
        )

    def collect(
        self,
        context: CollectionContext,
        emit: Callable[[ProgressEvent], None],
        is_cancel_requested: Callable[[], bool],
    ) -> CollectionResult:
        # `collect_site_audit` resolves through the module-level lazy seam.
        # Besides keeping the optional dependency lazy, this is the single
        # call point used by tests and alternate collectors.  Importing the
        # implementation again here bypassed that seam and made every mocked
        # collection silently hit the live adapter instead.
        from seo_audit.units_fallback import UnitsExhausted

        if is_cancel_requested():
            raise CancelledByAdapter("ahrefs: cancelled before collection started")

        domain = str(context.integration_config.get("domain", ""))
        csv_dir = context.integration_config.get("csv_dir")
        market = str(context.integration_config.get("market", ""))
        client_label = str(context.integration_config.get("ahrefs_client_label", ""))
        on_event = _bridge_emit(emit)

        api_allowed = context.source_mode in (SourceMode.api, SourceMode.auto)
        csv_allowed = bool(csv_dir) and context.source_mode in (SourceMode.browser, SourceMode.auto)

        result: DashboardResult | None = None
        fallback_used = False
        units_exhausted_detail: str | None = None

        if api_allowed and os.environ.get(AHREFS_API_KEY_ENV):
            try:
                result = collect_site_audit(
                    domain, client=client_label, market=market, csv_dir=None, on_event=on_event
                )
            except UnitsExhausted as exc:
                units_exhausted_detail = str(exc)

        needs_fallback = csv_allowed and (result is None or result.quality.value == "unavailable")
        if needs_fallback:
            if is_cancel_requested():
                raise CancelledByAdapter("ahrefs: cancelled before CSV fallback")
            try:
                csv_result = collect_site_audit(
                    domain, client=client_label, market=market, csv_dir=csv_dir, on_event=on_event
                )
            except UnitsExhausted:
                csv_result = None
            if csv_result is not None and (
                result is None or _dash_quality_rank(csv_result.quality.value)
                < _dash_quality_rank(result.quality.value)
            ):
                result = csv_result
                fallback_used = True

        if result is None:
            error_code = "no_units" if units_exhausted_detail else "collection_unavailable"
            warnings = (
                [f"Ahrefs API units exhausted: {units_exhausted_detail}"]
                if units_exhausted_detail
                else ["Ahrefs Site Audit: no data source available (no API key, no CSV fallback)"]
            )
            return CollectionResult(
                provider=self.provider,
                quality=Quality.unavailable,
                observed_at=datetime.now(UTC),
                metrics=[],
                warnings=warnings,
                error_code=error_code,
                provenance={"requested_source_mode": context.source_mode.value},
            )

        return self._to_collection_result(result, context, fallback_used=fallback_used)

    def _to_collection_result(
        self, dash_result: DashboardResult, context: CollectionContext, *, fallback_used: bool
    ) -> CollectionResult:
        crawl_date = dash_result.data_through
        is_stale = crawl_date is not None and crawl_date < context.current.start

        metrics: list[MetricValueResult] = []
        qualities: list[Quality] = []
        for value in dash_result.values:
            if value.value_numeric is None:
                quality = Quality.unavailable
            elif is_stale:
                quality = Quality.stale
            else:
                quality = Quality.complete
            qualities.append(quality)
            metrics.append(
                MetricValueResult(
                    metric_key=value.metric_key,
                    period_role=PeriodRole.snapshot,
                    value_numeric=value.value_numeric,
                    unit=MetricUnit(value.unit.value),
                    quality=quality,
                )
            )

        warnings = list(dash_result.warnings)
        if is_stale and crawl_date is not None:
            warnings.append(
                f"Ahrefs crawl finished {crawl_date.isoformat()}, before the current "
                f"period start {context.current.start.isoformat()} — metrics marked stale."
            )

        aggregate = (
            max(qualities, key=lambda q: _QUALITY_SEVERITY[q]) if qualities else Quality.unavailable
        )

        provenance = dict(dash_result.provenance)
        provenance["fallback_used"] = fallback_used
        provenance["requested_source_mode"] = context.source_mode.value

        return CollectionResult(
            provider=self.provider,
            quality=aggregate,
            observed_at=dash_result.observed_at,
            data_through=crawl_date,
            metrics=metrics,
            warnings=warnings,
            provenance=provenance,
            units_spent=dash_result.units_spent,
            units_estimated=dash_result.units_estimated,
        )


register_adapter(AhrefsAdapter())
