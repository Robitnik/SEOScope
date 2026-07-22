"""Aggregation of stored history into an arbitrary range (task-14, PLAN §21.1).

The tests that matter most here are the ones that would pass with obviously
wrong arithmetic: mean-of-CTRs, unweighted position, summed snapshots and
gaps silently reported as complete data.
"""

from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from app.metrics.aggregate import StoredMetricValue, aggregate_metrics
from app.schemas.adapter import DateRange
from app.schemas.enums import MetricUnit, PeriodRole, Provider, Quality
from app.schemas.metrics import MetricSpec

UTC = ZoneInfo("UTC")
CLIENT = "client-1"

APRIL = DateRange(start=date(2026, 4, 1), end=date(2026, 4, 28))


def _observed(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, 6, 0, tzinfo=UTC)


def _week(index: int) -> tuple[date, date]:
    """`index`-th 7-day block of `APRIL`, 0-based."""
    start = APRIL.start + timedelta(days=7 * index)
    return start, start + timedelta(days=6)


def _range_row(
    metric_key: str,
    period: tuple[date, date],
    value: Decimal | int | None,
    *,
    unit: MetricUnit = MetricUnit.count,
    quality: Quality = Quality.complete,
    provider: Provider = Provider.gsc,
    client_id: str = CLIENT,
    role: PeriodRole = PeriodRole.current,
    observed_at: datetime | None = None,
    is_derived: bool = False,
) -> StoredMetricValue:
    start, end = period
    return StoredMetricValue(
        client_id=client_id,
        provider=provider,
        metric_key=metric_key,
        period_role=role,
        period_start=start,
        period_end=end,
        value_numeric=None if value is None else Decimal(value),
        unit=unit,
        quality=quality,
        is_derived=is_derived,
        observed_at=observed_at or _observed(end),
    )


def _snapshot_row(
    metric_key: str,
    observed_on: date,
    value: Decimal | int | None,
    *,
    provider: Provider = Provider.ahrefs,
    unit: MetricUnit = MetricUnit.score,
    quality: Quality = Quality.complete,
    client_id: str = CLIENT,
) -> StoredMetricValue:
    return StoredMetricValue(
        client_id=client_id,
        provider=provider,
        metric_key=metric_key,
        period_role=PeriodRole.snapshot,
        value_numeric=None if value is None else Decimal(value),
        unit=unit,
        quality=quality,
        observed_at=_observed(observed_on),
    )


def _by_key(results):
    return {row.metric_key: row for row in results}


def _gsc(values, date_range: DateRange = APRIL, **kwargs):
    return aggregate_metrics(
        values, client_id=CLIENT, provider=Provider.gsc, date_range=date_range, **kwargs
    )


# --- additive metrics -----------------------------------------------------


def test_additive_metrics_are_summed_over_the_stored_sub_periods():
    values = [_range_row("gsc_clicks", _week(i), 100 * (i + 1)) for i in range(4)] + [
        _range_row("gsc_impressions", _week(i), 1000) for i in range(4)
    ]

    results, coverage = _gsc(values)
    metrics = _by_key(results)

    assert metrics["gsc_clicks"].value_numeric == Decimal(1000)  # 100+200+300+400
    assert metrics["gsc_impressions"].value_numeric == Decimal(4000)
    assert metrics["gsc_clicks"].quality == Quality.complete
    assert metrics["gsc_clicks"].period_start == APRIL.start
    assert metrics["gsc_clicks"].period_end == APRIL.end
    assert metrics["gsc_clicks"].period_role == PeriodRole.current
    assert coverage.quality == Quality.complete
    assert coverage.days_covered == coverage.days_total == 28
    assert coverage.gaps == []


def test_previous_role_is_applied_to_range_metrics():
    values = [_range_row("gsc_clicks", _week(i), 10) for i in range(4)]

    results, _ = _gsc(values, period_role=PeriodRole.previous)

    assert _by_key(results)["gsc_clicks"].period_role == PeriodRole.previous


def test_snapshot_role_cannot_be_requested():
    with pytest.raises(ValueError, match="assigned per metric"):
        _gsc([], period_role=PeriodRole.snapshot)


# --- CTR ------------------------------------------------------------------


def test_ctr_is_total_clicks_over_total_impressions_not_a_mean_of_ctrs():
    # Deliberately lopsided volumes: mean(10%, 0.1%) = 5.05%, the correct
    # answer is 110/11000 = 1.00%.
    values = [
        _range_row("gsc_clicks", _week(0), 100),
        _range_row("gsc_impressions", _week(0), 1000),
        _range_row("gsc_ctr", _week(0), Decimal("10.00"), unit=MetricUnit.percent),
        _range_row("gsc_clicks", _week(1), 10),
        _range_row("gsc_impressions", _week(1), 10_000),
        _range_row("gsc_ctr", _week(1), Decimal("0.10"), unit=MetricUnit.percent),
    ]

    results, _ = _gsc(values, date_range=DateRange(start=_week(0)[0], end=_week(1)[1]))
    ctr = _by_key(results)["gsc_ctr"]

    assert ctr.value_numeric == Decimal("1.00")
    assert ctr.value_numeric != Decimal("5.05")
    assert ctr.is_derived is True


def test_ctr_falls_back_to_a_stored_value_only_for_a_single_sub_period():
    week = _week(0)
    values = [_range_row("gsc_ctr", week, Decimal("4.25"), unit=MetricUnit.percent)]

    results, _ = _gsc(values, date_range=DateRange(start=week[0], end=week[1]))
    ctr = _by_key(results)["gsc_ctr"]

    assert ctr.value_numeric == Decimal("4.25")
    assert ctr.is_derived is True


def test_ctr_is_unavailable_when_components_are_missing_across_sub_periods():
    values = [
        _range_row("gsc_ctr", _week(0), Decimal("4.00"), unit=MetricUnit.percent),
        _range_row("gsc_ctr", _week(1), Decimal("8.00"), unit=MetricUnit.percent),
    ]

    results, _ = _gsc(values, date_range=DateRange(start=_week(0)[0], end=_week(1)[1]))
    ctr = _by_key(results)["gsc_ctr"]

    # Averaging two stored CTRs would give 6.00 — a number we cannot justify.
    assert ctr.value_numeric is None
    assert ctr.quality == Quality.unavailable


# --- avg position ---------------------------------------------------------


def test_avg_position_is_weighted_by_impressions():
    values = [
        _range_row("gsc_avg_position", _week(0), Decimal("5.0"), unit=MetricUnit.position),
        _range_row("gsc_impressions", _week(0), 1000),
        _range_row("gsc_avg_position", _week(1), Decimal("20.0"), unit=MetricUnit.position),
        _range_row("gsc_impressions", _week(1), 9000),
    ]

    results, _ = _gsc(values, date_range=DateRange(start=_week(0)[0], end=_week(1)[1]))
    position = _by_key(results)["gsc_avg_position"]

    # (5*1000 + 20*9000) / 10000 = 18.5; the plain mean would be 12.5.
    assert position.value_numeric == Decimal("18.5")
    assert position.value_numeric != Decimal("12.5")
    assert position.is_derived is True


def test_avg_position_without_weights_is_unavailable_never_a_plain_mean():
    values = [
        _range_row("gsc_avg_position", _week(0), Decimal("5.0"), unit=MetricUnit.position),
        _range_row("gsc_avg_position", _week(1), Decimal("20.0"), unit=MetricUnit.position),
    ]

    results, _ = _gsc(values, date_range=DateRange(start=_week(0)[0], end=_week(1)[1]))
    position = _by_key(results)["gsc_avg_position"]

    assert position.value_numeric is None
    assert position.quality == Quality.unavailable


def test_avg_position_for_a_single_sub_period_needs_no_weight():
    week = _week(0)
    values = [_range_row("gsc_avg_position", week, Decimal("7.3"), unit=MetricUnit.position)]

    results, _ = _gsc(values, date_range=DateRange(start=week[0], end=week[1]))

    assert _by_key(results)["gsc_avg_position"].value_numeric == Decimal("7.3")


# --- snapshot metrics -----------------------------------------------------


def test_snapshot_metrics_take_the_last_value_in_range_and_are_never_summed():
    values = [
        _snapshot_row("ahrefs_health_score", date(2026, 4, 5), 70),
        _snapshot_row("ahrefs_health_score", date(2026, 4, 20), 85),
        _snapshot_row("ahrefs_health_score", date(2026, 5, 3), 40),  # after the range
    ]

    results, coverage = aggregate_metrics(
        values, client_id=CLIENT, provider=Provider.ahrefs, date_range=APRIL
    )
    health = _by_key(results)["ahrefs_health_score"]

    assert health.value_numeric == Decimal(85)
    assert health.period_role == PeriodRole.snapshot
    assert health.period_start is None and health.period_end is None
    assert coverage.quality == Quality.complete


def test_snapshot_metric_outside_the_range_is_unavailable():
    values = [_snapshot_row("ahrefs_health_score", date(2026, 3, 20), 70)]

    results, coverage = aggregate_metrics(
        values, client_id=CLIENT, provider=Provider.ahrefs, date_range=APRIL
    )

    assert _by_key(results)["ahrefs_health_score"].value_numeric is None
    assert coverage.quality == Quality.unavailable
    assert coverage.days_covered == 0


def test_unavailable_snapshot_rows_are_skipped_in_favour_of_an_older_valid_one():
    values = [
        _snapshot_row("ahrefs_health_score", date(2026, 4, 5), 70),
        _snapshot_row("ahrefs_health_score", date(2026, 4, 25), None, quality=Quality.unavailable),
    ]

    results, _ = aggregate_metrics(
        values, client_id=CLIENT, provider=Provider.ahrefs, date_range=APRIL
    )

    assert _by_key(results)["ahrefs_health_score"].value_numeric == Decimal(70)


# --- coverage -------------------------------------------------------------


def test_a_hole_in_the_history_is_partial_with_the_exact_gap():
    values = [
        _range_row("gsc_clicks", _week(0), 100),
        _range_row("gsc_clicks", _week(1), 100),
        # week 2 (15–21 Apr) was never collected
        _range_row("gsc_clicks", _week(3), 100),
    ]

    results, coverage = _gsc(values)
    clicks = _by_key(results)["gsc_clicks"]

    assert coverage.quality == Quality.partial
    assert coverage.days_total == 28
    assert coverage.days_covered == 21
    assert coverage.gaps == [DateRange(start=date(2026, 4, 15), end=date(2026, 4, 21))]
    # The number itself is real, but it does not describe the whole range.
    assert clicks.value_numeric == Decimal(300)
    assert clicks.quality == Quality.partial


def test_partial_coverage_at_both_edges_of_the_range():
    values = [_range_row("gsc_clicks", _week(1), 100), _range_row("gsc_clicks", _week(2), 100)]

    _, coverage = _gsc(values)

    assert coverage.gaps == [
        DateRange(start=date(2026, 4, 1), end=date(2026, 4, 7)),
        DateRange(start=date(2026, 4, 22), end=date(2026, 4, 28)),
    ]
    assert coverage.days_covered == 14


def test_a_row_only_partially_overlapping_the_range_is_not_counted():
    outside = (date(2026, 3, 30), date(2026, 4, 5))
    values = [_range_row("gsc_clicks", outside, 500), _range_row("gsc_clicks", _week(1), 100)]

    results, coverage = _gsc(values)

    # Pro-rating the March week into April would be invented data.
    assert _by_key(results)["gsc_clicks"].value_numeric == Decimal(100)
    assert coverage.days_covered == 7
    assert coverage.gaps[0] == DateRange(start=date(2026, 4, 1), end=date(2026, 4, 7))


def test_empty_history_is_unavailable_and_never_zero():
    results, coverage = _gsc([])
    metrics = _by_key(results)

    assert set(metrics) == {"gsc_clicks", "gsc_impressions", "gsc_ctr", "gsc_avg_position"}
    for row in results:
        assert row.value_numeric is None
        assert row.quality == Quality.unavailable
    assert coverage.days_covered == 0
    assert coverage.days_total == 28
    assert coverage.quality == Quality.unavailable
    assert coverage.gaps == [APRIL]


def test_a_metric_missing_from_an_otherwise_covered_range_is_partial():
    values = [_range_row("gsc_clicks", _week(i), 100) for i in range(4)] + [
        _range_row("gsc_impressions", _week(0), 1000)
    ]

    results, coverage = _gsc(values)
    metrics = _by_key(results)

    assert coverage.quality == Quality.complete
    assert metrics["gsc_clicks"].quality == Quality.complete
    assert metrics["gsc_impressions"].value_numeric == Decimal(1000)
    assert metrics["gsc_impressions"].quality == Quality.partial


def test_a_stored_none_value_does_not_cover_its_days():
    values = [_range_row("gsc_clicks", _week(i), None) for i in range(4)]

    results, coverage = _gsc(values)

    assert _by_key(results)["gsc_clicks"].value_numeric is None
    assert coverage.quality == Quality.unavailable
    assert coverage.days_covered == 0


def test_worst_input_quality_wins():
    values = [
        _range_row("gsc_clicks", _week(i), 100, quality=Quality.complete) for i in range(3)
    ] + [_range_row("gsc_clicks", _week(3), 100, quality=Quality.truncated)]

    results, _ = _gsc(values)

    assert _by_key(results)["gsc_clicks"].quality == Quality.truncated


# --- overlapping and re-collected history ---------------------------------


def test_the_same_period_collected_twice_is_counted_once_newest_wins():
    week = _week(0)
    values = [
        _range_row("gsc_clicks", week, 100, observed_at=_observed(date(2026, 4, 8))),
        # A later re-collection corrected the same week to 120.
        _range_row("gsc_clicks", week, 120, observed_at=_observed(date(2026, 4, 15))),
    ]

    results, _ = _gsc(values, date_range=DateRange(start=week[0], end=week[1]))

    assert _by_key(results)["gsc_clicks"].value_numeric == Decimal(120)


def test_the_same_week_stored_as_current_and_as_previous_is_counted_once():
    week = _week(0)
    values = [
        _range_row("gsc_clicks", week, 100, role=PeriodRole.current),
        _range_row("gsc_clicks", week, 100, role=PeriodRole.previous),
    ]

    results, _ = _gsc(values, date_range=DateRange(start=week[0], end=week[1]))

    assert _by_key(results)["gsc_clicks"].value_numeric == Decimal(100)


def test_a_month_row_overlapping_week_rows_is_not_double_counted():
    whole = (APRIL.start, APRIL.end)
    values = [_range_row("gsc_clicks", whole, 1000)] + [
        _range_row("gsc_clicks", _week(i), 250) for i in range(4)
    ]

    results, coverage = _gsc(values)

    # The longest range wins the overlap; 1000 + 4*250 would be double counting.
    assert _by_key(results)["gsc_clicks"].value_numeric == Decimal(1000)
    assert coverage.quality == Quality.complete
    assert coverage.days_covered == 28


def test_sub_periods_are_shared_by_every_metric_so_derived_values_stay_consistent():
    # Impressions exist for the whole month as one row and for the weeks;
    # clicks only for the weeks. Both must aggregate over the same periods.
    whole = (APRIL.start, APRIL.end)
    values = (
        [_range_row("gsc_impressions", whole, 10_000)]
        + [_range_row("gsc_impressions", _week(i), 2500) for i in range(4)]
        + [_range_row("gsc_clicks", _week(i), 100) for i in range(4)]
    )

    results, _ = _gsc(values)
    metrics = _by_key(results)

    assert metrics["gsc_impressions"].value_numeric == Decimal(10_000)  # the month row
    # Clicks have nothing for the month row, so they are partial, and CTR is
    # not computed from mismatched spans.
    assert metrics["gsc_clicks"].value_numeric is None
    assert metrics["gsc_clicks"].quality == Quality.unavailable
    assert metrics["gsc_ctr"].value_numeric is None


# --- scoping --------------------------------------------------------------


def test_rows_of_other_clients_and_providers_are_ignored():
    values = [
        _range_row("gsc_clicks", _week(0), 100),
        _range_row("gsc_clicks", _week(1), 100, client_id="client-2"),
        _range_row("gsc_clicks", _week(2), 100, provider=Provider.reporting_ninja),
    ]

    results, coverage = _gsc(values)

    assert _by_key(results)["gsc_clicks"].value_numeric == Decimal(100)
    assert coverage.days_covered == 7


def test_a_range_metric_with_no_aggregation_rule_is_refused_not_summed():
    catalog = {
        "gsc_share_of_voice": MetricSpec(
            key="gsc_share_of_voice",
            label="Share of Voice",
            provider=Provider.gsc,
            unit=MetricUnit.percent,
            comparison_kind="range",
            better_direction="up",
            precision=2,
        )
    }
    values = [
        _range_row("gsc_share_of_voice", _week(i), Decimal("12.50"), unit=MetricUnit.percent)
        for i in range(4)
    ]

    results, _ = _gsc(values, catalog=catalog)

    # Summing percentages would produce 50.00 — nonsense.
    assert results[0].value_numeric is None
    assert results[0].quality == Quality.unavailable


def test_inverted_range_is_rejected():
    with pytest.raises(ValueError, match="precedes"):
        _gsc([], date_range=DateRange(start=date(2026, 4, 28), end=date(2026, 4, 1)))
