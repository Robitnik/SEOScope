from datetime import UTC, date, datetime
from decimal import Decimal

from app.metrics.normalize import normalize_result
from app.schemas.adapter import CollectionResult, MetricValueResult
from app.schemas.enums import MetricUnit, PeriodRole, Provider, Quality

CURRENT = (date(2026, 7, 13), date(2026, 7, 19))
PREVIOUS = (date(2026, 7, 6), date(2026, 7, 12))


def _result(metrics: list[MetricValueResult]) -> CollectionResult:
    return CollectionResult(
        provider=Provider.gsc,
        quality=Quality.complete,
        observed_at=datetime(2026, 7, 20, tzinfo=UTC),
        metrics=metrics,
    )


def _clicks(role: PeriodRole, value, quality=Quality.complete) -> MetricValueResult:
    start, end = CURRENT if role == PeriodRole.current else PREVIOUS
    return MetricValueResult(
        metric_key="gsc_clicks",
        period_role=role,
        period_start=start,
        period_end=end,
        value_numeric=value,
        unit=MetricUnit.count,
        quality=quality,
    )


def _impressions(role: PeriodRole, value, quality=Quality.complete) -> MetricValueResult:
    start, end = CURRENT if role == PeriodRole.current else PREVIOUS
    return MetricValueResult(
        metric_key="gsc_impressions",
        period_role=role,
        period_start=start,
        period_end=end,
        value_numeric=value,
        unit=MetricUnit.count,
        quality=quality,
    )


def test_rounds_value_to_catalog_precision():
    metrics = [
        MetricValueResult(
            metric_key="gsc_avg_position",
            period_role=PeriodRole.current,
            period_start=CURRENT[0],
            period_end=CURRENT[1],
            value_numeric=Decimal("12.348"),
            unit=MetricUnit.position,
            quality=Quality.complete,
        )
    ]

    normalized = normalize_result(_result(metrics))

    assert normalized[0].value_numeric == Decimal("12.3")


def test_none_value_stays_none_and_is_never_rounded_to_zero():
    metrics = [
        MetricValueResult(
            metric_key="gsc_avg_position",
            period_role=PeriodRole.current,
            period_start=CURRENT[0],
            period_end=CURRENT[1],
            value_numeric=None,
            unit=MetricUnit.position,
            quality=Quality.unavailable,
        )
    ]

    normalized = normalize_result(_result(metrics))

    assert normalized[0].value_numeric is None


def test_derived_ctr_computed_when_components_complete():
    metrics = [
        _clicks(PeriodRole.current, Decimal("100")),
        _impressions(PeriodRole.current, Decimal("1000")),
        _clicks(PeriodRole.previous, Decimal("50")),
        _impressions(PeriodRole.previous, Decimal("1000")),
    ]

    normalized = normalize_result(_result(metrics))
    derived = {(m.metric_key, m.period_role): m for m in normalized if m.is_derived}

    assert derived[("gsc_ctr", PeriodRole.current)].value_numeric == Decimal("10.00")
    assert derived[("gsc_ctr", PeriodRole.current)].quality == Quality.complete
    assert derived[("gsc_ctr", PeriodRole.previous)].value_numeric == Decimal("5.00")


def test_derived_ctr_skipped_when_a_component_is_not_complete():
    metrics = [
        _clicks(PeriodRole.current, Decimal("100"), quality=Quality.partial),
        _impressions(PeriodRole.current, Decimal("1000")),
    ]

    normalized = normalize_result(_result(metrics))

    assert not any(m.is_derived for m in normalized)


def test_derived_ctr_not_duplicated_when_adapter_already_supplied_it():
    explicit_ctr = MetricValueResult(
        metric_key="gsc_ctr",
        period_role=PeriodRole.current,
        period_start=CURRENT[0],
        period_end=CURRENT[1],
        value_numeric=Decimal("9.99"),
        unit=MetricUnit.percent,
        quality=Quality.complete,
    )
    metrics = [
        explicit_ctr,
        _clicks(PeriodRole.current, Decimal("100")),
        _impressions(PeriodRole.current, Decimal("1000")),
    ]

    normalized = normalize_result(_result(metrics))
    ctr_values = [m for m in normalized if m.metric_key == "gsc_ctr"]

    assert len(ctr_values) == 1
    assert ctr_values[0].value_numeric == Decimal("9.99")
    assert ctr_values[0].is_derived is False


def test_unknown_metric_key_passes_through_unchanged():
    metrics = [
        MetricValueResult(
            metric_key="some_future_metric",
            period_role=PeriodRole.current,
            period_start=CURRENT[0],
            period_end=CURRENT[1],
            value_numeric=Decimal("1.23456"),
            unit=MetricUnit.count,
            quality=Quality.complete,
        )
    ]

    normalized = normalize_result(_result(metrics))

    assert normalized[0].value_numeric == Decimal("1.23456")
