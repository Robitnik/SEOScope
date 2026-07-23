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
from app.schemas.analytics_datasets import (
    AnalyticsDatasetCollectionResult,
    AnalyticsDatasetValueResult,
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


def collect_site_audit_browser(
    domain: str,
    *,
    client: str,
    market: str,
    on_event: Callable[[DashboardProgressEvent], None],
) -> DashboardResult:
    """Lazy seam over the AISeoTools BROWSER Site Audit collector (no API, no units).

    Same optional-dependency discipline as `collect_site_audit`. Tests monkeypatch this name
    (`adapter_module.collect_site_audit_browser`)."""
    from seo_audit.dashboard.ahrefs import collect_site_audit_browser as _impl

    return _impl(domain, client=client, market=market, on_event=on_event)


def collect_site_audit_detail_browser(
    domain: str,
    *,
    emit: Callable[[ProgressEvent], None],
) -> dict[str, object]:
    """Export and parse every Site Audit issue and its affected page rows.

    This deliberately lives behind a lazy seam: AISeoTools is optional in CI,
    while live installations attach to its already-authenticated warm Chrome.
    The bulk Ahrefs ZIP contains one CSV per issue, including all issue-specific
    columns; those raw columns are retained in ``details`` rather than discarded.
    """
    from seo_audit.ahrefs_audit.csv_parser import parse_directory
    from seo_audit.browser.profile import download_capture_dir
    from seo_audit.browser.providers.ahrefs import AhrefsSiteAudit
    from seo_audit.browser.session import BrowserSession
    from seo_audit.framework.ahrefs_parser import _read_csv

    emit(
        ProgressEvent(
            provider=Provider.ahrefs,
            stage="export_issues",
            message=f"Exporting all Ahrefs Site Audit issues for {domain}",
        )
    )
    downloads = download_capture_dir()
    downloads.mkdir(parents=True, exist_ok=True)
    with BrowserSession("ahrefs") as session:
        audit = AhrefsSiteAudit(session)
        project_id = audit.resolve_project(domain)
        if not project_id:
            raise LookupError(f"Ahrefs Site Audit project not found for {domain}")
        zip_path = audit.export_all_issues_zip(project_id, downloads=downloads)
        unpacked = audit.unpack(zip_path, downloads)

    emit(
        ProgressEvent(
            provider=Provider.ahrefs,
            stage="parse_issues",
            message="Parsing Ahrefs issues and affected URLs",
        )
    )
    parsed = parse_directory(downloads, domain=domain)
    issue_payloads: list[dict[str, object]] = []
    for issue in parsed.sorted_issues():
        affected_rows: list[dict[str, object]] = []
        affected_path = downloads / f"affected_{issue.slug}.csv"
        if affected_path.is_file():
            for raw in _read_csv(affected_path):
                details = {str(key): str(value or "") for key, value in raw.items()}
                url = next(
                    (
                        value
                        for key, value in details.items()
                        if value
                        and any(
                            token in key.casefold() for token in ("url", "page", "address")
                        )
                    ),
                    "",
                )
                affected_rows.append({"url": url, "details": details})
        issue_payloads.append(
            {
                "slug": issue.slug,
                "issue_id": issue.issue_id,
                "name": issue.name,
                "severity": issue.severity,
                "priority": issue.priority.value,
                "description": issue.description,
                "how_to_fix": issue.how_to_fix,
                "url_count": issue.url_count,
                "affected": affected_rows,
            }
        )
    return {
        "project_id": project_id,
        "issues": issue_payloads,
        "warnings": list(parsed.warnings),
        "export_issue_count": int(unpacked.get("issues", len(issue_payloads))),
    }


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

        if context.source_mode == SourceMode.pdf:
            return PreflightResult(
                provider=self.provider,
                ok=False,
                reason_code="not_configured",
                message="Ahrefs does not support source_mode=pdf",
            )

        # browser or auto → live Site Audit overview scrape (no API units). The warm Ahrefs
        # session's liveness isn't cheaply checkable here, so preflight just clears the way; a
        # missing/expired session surfaces as an `unavailable` result at collect time, same as the
        # other browser providers. `csv_dir`, if present, remains a fallback (see `collect`).
        if context.source_mode in (SourceMode.browser, SourceMode.auto):
            return PreflightResult(provider=self.provider, ok=True, units_estimated=0)

        # explicit api mode only
        if os.environ.get(AHREFS_API_KEY_ENV):
            return PreflightResult(provider=self.provider, ok=True, units_estimated=_UNITS_ESTIMATE)
        return PreflightResult(
            provider=self.provider,
            ok=False,
            reason_code="auth_required",
            message=f"source_mode=api but {AHREFS_API_KEY_ENV} is not set",
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

        result: DashboardResult | None = None
        fallback_used = False
        units_exhausted_detail: str | None = None

        # Default path is the BROWSER Site Audit scrape (source_mode auto/browser) — no API, no
        # units. Only an EXPLICIT source_mode=api hits the metered API. This is deliberate: the
        # operator's directive is browser-only, and the API is units-gated (exhausted for long
        # stretches), so it must never be the silent default.
        if context.source_mode in (SourceMode.auto, SourceMode.browser):
            result = collect_site_audit_browser(
                domain, client=client_label, market=market, on_event=on_event
            )
            # csv_dir remains a fallback if the live scrape produced nothing usable.
            if bool(csv_dir) and (result is None or result.quality.value == "unavailable"):
                if is_cancel_requested():
                    raise CancelledByAdapter("ahrefs: cancelled before CSV fallback")
                try:
                    csv_result = collect_site_audit(
                        domain, client=client_label, market=market, csv_dir=csv_dir,
                        on_event=on_event,
                    )
                except UnitsExhausted:
                    csv_result = None
                if csv_result is not None and (
                    result is None or _dash_quality_rank(csv_result.quality.value)
                    < _dash_quality_rank(result.quality.value)
                ):
                    result = csv_result
                    fallback_used = True
        elif context.source_mode == SourceMode.api and os.environ.get(AHREFS_API_KEY_ENV):
            try:
                result = collect_site_audit(
                    domain, client=client_label, market=market, csv_dir=None, on_event=on_event
                )
            except UnitsExhausted as exc:
                units_exhausted_detail = str(exc)

        if result is None:
            error_code = "no_units" if units_exhausted_detail else "collection_unavailable"
            warnings = (
                [f"Ahrefs API units exhausted: {units_exhausted_detail}"]
                if units_exhausted_detail
                else ["Ahrefs Site Audit: no data source available"]
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

    def collect_datasets(
        self,
        context: CollectionContext,
        emit: Callable[[ProgressEvent], None],
    ) -> AnalyticsDatasetCollectionResult:
        """Collect the complete live Site Audit issue/affected-URL export."""
        observed_at = datetime.now(UTC)
        if context.source_mode not in (SourceMode.auto, SourceMode.browser):
            return AnalyticsDatasetCollectionResult(
                provider=self.provider,
                quality=Quality.unavailable,
                observed_at=observed_at,
                warnings=["Detailed Ahrefs Site Audit requires source_mode=auto or browser"],
                error_code="browser_required",
            )
        domain = str(context.integration_config.get("domain", ""))
        try:
            detail = collect_site_audit_detail_browser(domain, emit=emit)
        except Exception as exc:  # noqa: BLE001 - optional detail never loses overview KPIs
            return AnalyticsDatasetCollectionResult(
                provider=self.provider,
                quality=Quality.unavailable,
                observed_at=observed_at,
                warnings=[f"Ahrefs detail export failed: {type(exc).__name__}"],
                error_code="detail_export_failed",
            )

        rows: list[AnalyticsDatasetValueResult] = []
        issues = detail.get("issues")
        if not isinstance(issues, list):
            issues = []
        affected_rank = 0
        for issue_rank, raw_issue in enumerate(issues, start=1):
            if not isinstance(raw_issue, dict):
                continue
            slug = str(raw_issue.get("slug") or f"issue-{issue_rank}")
            affected = raw_issue.get("affected")
            affected_rows = affected if isinstance(affected, list) else []
            rows.append(
                AnalyticsDatasetValueResult(
                    dataset_key="ahrefs_issues",
                    dimension_key="issue_slug",
                    dimension_value=slug,
                    period_role=PeriodRole.current,
                    period_start=context.current.start,
                    period_end=context.current.end,
                    metrics={
                        "issue_id": str(raw_issue.get("issue_id") or ""),
                        "name": str(raw_issue.get("name") or slug),
                        "severity": str(raw_issue.get("severity") or "Notice"),
                        "priority": str(raw_issue.get("priority") or "low"),
                        "description": str(raw_issue.get("description") or ""),
                        "how_to_fix": str(raw_issue.get("how_to_fix") or ""),
                        "affected_urls": int(raw_issue.get("url_count") or len(affected_rows)),
                        "exported_rows": len(affected_rows),
                    },
                    quality=Quality.complete,
                    rank=issue_rank,
                )
            )
            for url_rank, raw_affected in enumerate(affected_rows, start=1):
                if not isinstance(raw_affected, dict):
                    continue
                affected_rank += 1
                url = str(raw_affected.get("url") or "")
                details = raw_affected.get("details")
                rows.append(
                    AnalyticsDatasetValueResult(
                        dataset_key="ahrefs_affected_urls",
                        dimension_key="issue_url",
                        dimension_value=f"{slug}\x1f{url_rank}\x1f{url}",
                        period_role=PeriodRole.current,
                        period_start=context.current.start,
                        period_end=context.current.end,
                        metrics={
                            "url": url,
                            "issue_slug": slug,
                            "issue_name": str(raw_issue.get("name") or slug),
                            "severity": str(raw_issue.get("severity") or "Notice"),
                            "details": details if isinstance(details, dict) else {},
                        },
                        quality=Quality.complete,
                        rank=affected_rank,
                    )
                )

        warnings = detail.get("warnings")
        quality = Quality.complete if issues else Quality.partial
        emit(
            ProgressEvent(
                provider=Provider.ahrefs,
                stage="parse_issues",
                completed=len(issues),
                total=len(issues),
                percent=100,
                message=f"Parsed {len(issues)} issue types and {affected_rank} affected rows",
            )
        )
        return AnalyticsDatasetCollectionResult(
            provider=self.provider,
            quality=quality,
            observed_at=observed_at,
            rows=rows,
            warnings=[str(item) for item in warnings] if isinstance(warnings, list) else [],
            provenance={
                "transport": "ahrefs_browser_bulk_zip",
                "project_id": detail.get("project_id"),
                "datasets": ["ahrefs_issues", "ahrefs_affected_urls"],
                "issue_types": len(issues),
                "affected_rows": affected_rank,
            },
        )

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
