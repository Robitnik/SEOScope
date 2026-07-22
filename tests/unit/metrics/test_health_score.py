from __future__ import annotations

from decimal import Decimal

from app.metrics.catalog import CATALOG
from app.metrics.health import compute_health
from app.schemas.api import MetricCardValue, ProviderSummaryDTO
from app.schemas.enums import Provider, Quality, SourceMode


def _metric(
    key: str,
    current: float | None,
    previous: float | None,
    *,
    quality: Quality = Quality.complete,
) -> MetricCardValue:
    spec = CATALOG[key]
    return MetricCardValue(
        metric_key=key,
        label=spec.label,
        unit=spec.unit,
        comparison_kind=spec.comparison_kind,
        precision=spec.precision,
        better_direction=spec.better_direction,
        current_value=Decimal(str(current)) if current is not None else None,
        previous_value=Decimal(str(previous)) if previous is not None else None,
        quality=quality,
        source_mode=SourceMode.api,
    )


def _provider(
    provider: Provider, state: str, metrics: list[MetricCardValue]
) -> ProviderSummaryDTO:
    return ProviderSummaryDTO(
        provider=provider,
        state=state,  # type: ignore[arg-type]
        metrics=metrics,
        quality=Quality.complete if state == "complete" else None,
    )


def test_ahrefs_errors_decrease_improves_health():
    summaries = [_provider(Provider.ahrefs, "complete", [_metric("ahrefs_errors", 5, 10)])]

    result = compute_health(summaries)

    assert result.value is not None
    assert result.value > 50


def test_gsc_clicks_decrease_lowers_health():
    summaries = [_provider(Provider.gsc, "complete", [_metric("gsc_clicks", 50, 100)])]

    result = compute_health(summaries)

    assert result.value is not None
    assert result.value < 50


def test_metric_without_previous_or_partial_does_not_drag_score():
    good = _metric("gsc_clicks", 120, 100)  # +20% -> score 70
    no_previous = _metric("gsc_impressions", 10, None)
    partial = _metric("rn_users", 1, 1000, quality=Quality.partial)

    only_good = compute_health([_provider(Provider.gsc, "complete", [good])])
    mixed = compute_health(
        [_provider(Provider.gsc, "complete", [good, no_previous, partial])]
    )

    assert mixed.value == only_good.value == Decimal(70)


def test_no_complete_service_yields_none_never_zero():
    summaries = [_provider(Provider.gsc, "partial", [_metric("gsc_clicks", 100, 80)])]

    result = compute_health(summaries)

    assert result.value is None
    assert result.contributing == 0


def test_neutral_direction_metric_excluded_from_scoring():
    summaries = [_provider(Provider.ahrefs, "complete", [_metric("ahrefs_urls_crawled", 500, 100)])]

    result = compute_health(summaries)

    assert result.value is None
    assert result.contributing == 0


def test_outlier_change_is_clamped_not_broken():
    summaries = [_provider(Provider.gsc, "complete", [_metric("gsc_clicks", 600, 100)])]  # +500%

    result = compute_health(summaries)

    assert result.value == Decimal(100)


def test_ahrefs_health_score_enters_as_absolute_value_not_delta():
    # previous is a wildly different number; only the current absolute score
    # (already 0..100) should matter.
    summaries = [_provider(Provider.ahrefs, "complete", [_metric("ahrefs_health_score", 82, 5)])]

    result = compute_health(summaries)

    assert result.value == Decimal(82)


def test_coverage_counts_only_configured_services():
    summaries = [
        _provider(Provider.gsc, "complete", [_metric("gsc_clicks", 110, 100)]),
        _provider(Provider.semrush, "not_configured", []),
    ]

    result = compute_health(summaries)

    assert result.configured == 1
    assert result.contributing == 1
