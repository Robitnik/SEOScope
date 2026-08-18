"""Portfolio Trends engine (task-28): bucketing + per-client aggregation.

The tests that matter most are the ones a naive implementation gets wrong:
absence rendered as `0`, a real `0` erased as absence, a clipped edge bucket
mislabeled partial just for being clipped, and the provisional/lag cutoff.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from app.metrics.portfolio import (
    PORTFOLIO_SERIES,
    aggregate_client_series,
    build_buckets,
)
from app.schemas.enums import Quality

# --- build_buckets: day -------------------------------------------------


def test_day_buckets_are_one_per_calendar_day():
    buckets = build_buckets(
        date(2026, 7, 1), date(2026, 7, 3), "day", today=date(2026, 7, 27), lag_days=3
    )

    assert [(b.start, b.end) for b in buckets] == [
        (date(2026, 7, 1), date(2026, 7, 1)),
        (date(2026, 7, 2), date(2026, 7, 2)),
        (date(2026, 7, 3), date(2026, 7, 3)),
    ]
    assert all(b.days_expected == 1 for b in buckets)


# --- build_buckets: week -------------------------------------------------


def test_week_buckets_start_on_monday():
    # 2026-07-01 is a Wednesday; the first (clipped) bucket starts at the
    # window edge, not Monday. The second bucket is a full, unclipped week
    # (Mon 2026-07-06 .. Sun 2026-07-12) and must start on Monday.
    buckets = build_buckets(
        date(2026, 7, 1), date(2026, 7, 12), "week", today=date(2026, 8, 1), lag_days=3
    )

    assert buckets[0].start == date(2026, 7, 1)
    assert buckets[1].start.weekday() == 0
    assert buckets[1].start == date(2026, 7, 6)
    assert buckets[1].end == date(2026, 7, 12)
    assert buckets[1].days_expected == 7


def test_edge_week_clipped_by_window_is_not_penalized_as_partial():
    """A window starting mid-week must not call that first bucket 'partial'
    just because it is shorter than 7 days — `days_expected` reflects the
    clipped span, not a fixed 7 (plan §3.4)."""
    buckets = build_buckets(
        date(2026, 7, 1),  # Wednesday
        date(2026, 7, 5),  # Sunday, same ISO week
        "week",
        today=date(2026, 8, 1),
        lag_days=3,
    )

    assert len(buckets) == 1
    assert buckets[0].start == date(2026, 7, 1)
    assert buckets[0].end == date(2026, 7, 5)
    assert buckets[0].days_expected == 5

    daily = {d: Decimal(10) for d in (date(2026, 7, 1), date(2026, 7, 2), date(2026, 7, 3),
                                       date(2026, 7, 4), date(2026, 7, 5))}
    aggregate = aggregate_client_series(daily, buckets)
    assert aggregate.points[0].quality == Quality.complete  # all 5 of 5 expected days present


# --- build_buckets: month -------------------------------------------------


def test_month_buckets_split_on_calendar_boundaries():
    buckets = build_buckets(
        date(2026, 1, 15), date(2026, 3, 10), "month", today=date(2026, 4, 1), lag_days=2
    )

    assert [(b.start, b.end) for b in buckets] == [
        (date(2026, 1, 15), date(2026, 1, 31)),
        (date(2026, 2, 1), date(2026, 2, 28)),
        (date(2026, 3, 1), date(2026, 3, 10)),
    ]
    assert buckets[0].days_expected == 17
    assert buckets[1].days_expected == 28
    assert buckets[2].days_expected == 10


def test_month_boundary_handles_december_to_january():
    buckets = build_buckets(
        date(2026, 12, 1), date(2027, 1, 5), "month", today=date(2027, 2, 1), lag_days=2
    )

    assert [(b.start, b.end) for b in buckets] == [
        (date(2026, 12, 1), date(2026, 12, 31)),
        (date(2027, 1, 1), date(2027, 1, 5)),
    ]


# --- provisional / lag --------------------------------------------------


def test_bucket_touching_the_lag_window_is_provisional():
    today = date(2026, 7, 27)
    buckets = build_buckets(
        date(2026, 7, 20), date(2026, 7, 27), "day", today=today, lag_days=3
    )
    by_day = {b.start: b for b in buckets}

    # lag_days=3 -> cutoff = today - 3 = 2026-07-24; anything on/after that is provisional.
    assert by_day[date(2026, 7, 23)].provisional is False
    assert by_day[date(2026, 7, 24)].provisional is True
    assert by_day[date(2026, 7, 27)].provisional is True


# --- aggregate_client_series: the None-vs-zero rule ------------------------


def test_missing_day_is_none_never_zero():
    buckets = build_buckets(
        date(2026, 7, 1), date(2026, 7, 3), "day", today=date(2026, 8, 1), lag_days=3
    )
    daily = {date(2026, 7, 1): Decimal(50)}  # days 2 and 3 never collected

    aggregate = aggregate_client_series(daily, buckets)

    assert aggregate.points[0].value == Decimal(50)
    assert aggregate.points[1].value is None
    assert aggregate.points[1].days_with_data == 0
    assert aggregate.points[1].quality == Quality.unavailable
    assert aggregate.points[2].value is None


def test_real_zero_is_preserved_not_treated_as_missing():
    buckets = build_buckets(
        date(2026, 7, 1), date(2026, 7, 1), "day", today=date(2026, 8, 1), lag_days=3
    )
    daily = {date(2026, 7, 1): Decimal(0)}  # a genuine zero-click day

    aggregate = aggregate_client_series(daily, buckets)

    assert aggregate.points[0].value == Decimal(0)
    assert aggregate.points[0].days_with_data == 1
    assert aggregate.points[0].quality == Quality.complete


def test_partial_week_is_flagged_and_sums_only_present_days():
    buckets = build_buckets(
        date(2026, 7, 6), date(2026, 7, 12), "week", today=date(2026, 8, 1), lag_days=3
    )  # full Mon-Sun week, days_expected=7
    daily = {
        date(2026, 7, 6): Decimal(10),
        date(2026, 7, 7): Decimal(20),
        date(2026, 7, 8): Decimal(30),
        date(2026, 7, 9): Decimal(40),
        # 10, 11, 12 never collected
    }

    aggregate = aggregate_client_series(daily, buckets)

    assert aggregate.points[0].days_with_data == 4
    assert aggregate.points[0].value == Decimal(100)
    assert aggregate.points[0].quality == Quality.partial


def test_client_with_zero_total_days_reports_no_first_or_last_day():
    buckets = build_buckets(
        date(2026, 7, 1), date(2026, 7, 3), "day", today=date(2026, 8, 1), lag_days=3
    )

    aggregate = aggregate_client_series({}, buckets)

    assert aggregate.days_with_data == 0
    assert aggregate.first_day is None
    assert aggregate.last_day is None
    assert all(point.value is None for point in aggregate.points)


def test_first_and_last_day_reflect_actual_collected_range():
    buckets = build_buckets(
        date(2026, 7, 1), date(2026, 7, 10), "day", today=date(2026, 8, 1), lag_days=3
    )
    daily = {date(2026, 7, 3): Decimal(1), date(2026, 7, 8): Decimal(2)}

    aggregate = aggregate_client_series(daily, buckets)

    assert aggregate.first_day == date(2026, 7, 3)
    assert aggregate.last_day == date(2026, 7, 8)
    assert aggregate.days_with_data == 2
    assert aggregate.days_expected == 10


def test_bucket_sum_equals_sum_of_daily_values():
    buckets = build_buckets(
        date(2026, 7, 6), date(2026, 7, 12), "week", today=date(2026, 8, 1), lag_days=3
    )
    daily = {date(2026, 7, 6) + timedelta(days=i): Decimal(i + 1) for i in range(7)}

    aggregate = aggregate_client_series(daily, buckets)

    assert aggregate.points[0].value == sum(daily.values(), Decimal(0))
    assert aggregate.points[0].quality == Quality.complete


# --- registry --------------------------------------------------------------


def test_portfolio_series_registry_has_the_seven_expected_metrics():
    assert set(PORTFOLIO_SERIES) == {
        "gsc_clicks",
        "gsc_impressions",
        "rn_sessions",
        "rn_users",
        "rn_total_conversions",
        "rn_organic_conversions",
        "rn_direct_conversions",
    }


def test_gsc_and_rn_specs_reuse_catalog_labels_where_available():
    from app.metrics.catalog import CATALOG

    for key in (
        "gsc_clicks",
        "gsc_impressions",
        "rn_users",
        "rn_total_conversions",
        "rn_organic_conversions",
    ):
        spec = PORTFOLIO_SERIES[key]
        assert spec.label == CATALOG[key].label
        assert spec.unit == CATALOG[key].unit
        assert spec.better_direction == CATALOG[key].better_direction


# --- task-44: additive flag + new conversion series -------------------------


def test_additive_flag_is_true_for_every_metric_except_users():
    for key, spec in PORTFOLIO_SERIES.items():
        expected = key != "rn_users"
        assert spec.additive is expected, f"{key}.additive should be {expected}"


def test_conversion_series_point_to_the_right_dataset_and_field():
    assert PORTFOLIO_SERIES["rn_total_conversions"].dataset_key == "ga4_daily"
    assert PORTFOLIO_SERIES["rn_total_conversions"].field == "conversions"
    assert PORTFOLIO_SERIES["rn_organic_conversions"].dataset_key == "ga4_daily_channels"
    assert PORTFOLIO_SERIES["rn_organic_conversions"].field == "conversions_organic"
    assert PORTFOLIO_SERIES["rn_direct_conversions"].dataset_key == "ga4_daily_channels"
    assert PORTFOLIO_SERIES["rn_direct_conversions"].field == "conversions_direct"


def test_rn_direct_conversions_has_no_catalog_entry_but_a_sensible_label():
    from app.metrics.catalog import CATALOG

    assert "rn_direct_conversions" not in CATALOG
    assert PORTFOLIO_SERIES["rn_direct_conversions"].label == "Direct Conversions"


def test_conversions_sum_correctly_into_a_month_bucket_like_the_client_report():
    """The accepted gate (task-44 plan §6): a month bucket must equal the
    matching column in the client report — verified live at 654/391/1099 for
    EFFYS/July 2026. This is the local, deterministic version of that check:
    summing daily conversions into one bucket reproduces the period total."""
    buckets = build_buckets(
        date(2026, 7, 1), date(2026, 7, 31), "month", today=date(2026, 8, 17), lag_days=2
    )
    daily = {date(2026, 7, 1) + timedelta(days=i): Decimal(20 + i) for i in range(31)}

    aggregate = aggregate_client_series(daily, buckets)

    assert aggregate.points[0].value == sum(daily.values(), Decimal(0))
    assert aggregate.points[0].quality == Quality.complete
