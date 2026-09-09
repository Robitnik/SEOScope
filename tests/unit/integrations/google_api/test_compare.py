"""Pure transport-comparison logic (task-83 §7)."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from decimal import Decimal

from app.integrations.google_api.compare import compare_dimension_rows, compare_series

TODAY = date(2026, 9, 7)


def _series(
    **days: Mapping[str, Decimal | None],
) -> dict[date, dict[str, Decimal | None]]:
    return {date.fromisoformat(d): dict(v) for d, v in days.items()}


def test_identical_integer_series_passes() -> None:
    a = _series(**{"2026-08-01": {"clicks": Decimal(10), "impressions": Decimal(100),
                                  "ctr": Decimal("0.1"), "position": Decimal("3")}})
    report = compare_series(
        "gsc_daily", a, dict(a), label_a="rn", label_b="ga", lag_days=3, today=TODAY
    )
    assert report.passed
    assert report.days_compared == 1
    assert all(m.failures == 0 for m in report.per_metric)


def test_one_click_off_fails_exact_metric() -> None:
    a = _series(**{"2026-08-01": {"clicks": Decimal(10), "impressions": Decimal(100),
                                  "ctr": Decimal("0.1"), "position": Decimal("3")}})
    b = _series(**{"2026-08-01": {"clicks": Decimal(11), "impressions": Decimal(100),
                                  "ctr": Decimal("0.1"), "position": Decimal("3")}})
    report = compare_series("gsc_daily", a, b, label_a="rn", label_b="ga", lag_days=3, today=TODAY)
    assert not report.passed
    assert [f.metric for f in report.failures] == ["clicks"]
    assert report.failures[0].abs_diff == Decimal(1)


def test_small_relative_drift_within_tolerance_passes() -> None:
    a = _series(**{"2026-08-01": {"sessions": Decimal(1000), "users": Decimal(800),
                                  "conversions": Decimal(10), "engagement_rate": Decimal(70)}})
    b = _series(**{"2026-08-01": {"sessions": Decimal(1005), "users": Decimal(800),
                                  "conversions": Decimal(10), "engagement_rate": Decimal(70)}})
    report = compare_series("ga4_daily", a, b, label_a="rn", label_b="ga", lag_days=2, today=TODAY)
    assert report.passed  # 0.5% < 1% tolerance


def test_lag_days_are_excluded_from_comparison() -> None:
    a = _series(**{
        "2026-09-06": {"clicks": Decimal(1), "impressions": Decimal(1),
                       "ctr": Decimal("1"), "position": Decimal("1")},  # inside 3d lag
        "2026-08-01": {"clicks": Decimal(1), "impressions": Decimal(1),
                       "ctr": Decimal("1"), "position": Decimal("1")},
    })
    b = _series(**{
        "2026-09-06": {"clicks": Decimal(999), "impressions": Decimal(999),
                       "ctr": Decimal("1"), "position": Decimal("1")},  # would fail if compared
        "2026-08-01": {"clicks": Decimal(1), "impressions": Decimal(1),
                       "ctr": Decimal("1"), "position": Decimal("1")},
    })
    report = compare_series("gsc_daily", a, b, label_a="rn", label_b="ga", lag_days=3, today=TODAY)
    assert report.days_compared == 1
    assert report.passed


def test_day_only_on_one_side_is_reported_and_fails_gate() -> None:
    a = _series(**{"2026-08-01": {"clicks": Decimal(1), "impressions": Decimal(1),
                                  "ctr": Decimal("1"), "position": Decimal("1")}})
    b = _series(**{
        "2026-08-01": {"clicks": Decimal(1), "impressions": Decimal(1),
                       "ctr": Decimal("1"), "position": Decimal("1")},
        "2026-08-02": {"clicks": Decimal(1), "impressions": Decimal(1),
                       "ctr": Decimal("1"), "position": Decimal("1")},
    })
    report = compare_series("gsc_daily", a, b, label_a="rn", label_b="ga", lag_days=3, today=TODAY)
    assert report.days_only_b == [date(2026, 8, 2)]
    assert not report.passed


def test_none_on_one_side_only_is_a_failure() -> None:
    a = _series(**{"2026-08-01": {"conversions_organic": Decimal(5), "conversions_direct": None}})
    b = _series(
        **{"2026-08-01": {"conversions_organic": Decimal(5), "conversions_direct": Decimal(2)}}
    )
    report = compare_series(
        "ga4_daily_channels", a, b, label_a="rn", label_b="ga", lag_days=2, today=TODAY
    )
    assert not report.passed
    assert report.failures[0].metric == "conversions_direct"
    assert "only in" in report.failures[0].note


def test_both_none_is_silently_skipped() -> None:
    a = _series(**{"2026-08-01": {"conversions_organic": Decimal(5), "conversions_direct": None}})
    b = _series(**{"2026-08-01": {"conversions_organic": Decimal(5), "conversions_direct": None}})
    report = compare_series(
        "ga4_daily_channels", a, b, label_a="rn", label_b="ga", lag_days=2, today=TODAY
    )
    assert report.passed
    direct = next(m for m in report.per_metric if m.metric == "conversions_direct")
    assert direct.compared == 0


# ── task-89 §7.1: compare_dimension_rows (pages/queries/GA4 breakdowns/KPI) ─
def test_dimension_rows_identical_passes() -> None:
    a = {"/blog/a": {"clicks": Decimal(10), "impressions": Decimal(100),
                      "ctr": Decimal("0.1"), "position": Decimal("3")}}
    report = compare_dimension_rows("gsc_pages", a, dict(a), label_a="rn", label_b="ga")
    assert report.passed
    assert report.keys_compared == 1


def test_dimension_rows_one_click_off_fails_exact_metric() -> None:
    a = {"/blog/a": {"clicks": Decimal(10), "impressions": Decimal(100),
                      "ctr": Decimal("0.1"), "position": Decimal("3")}}
    b = {"/blog/a": {"clicks": Decimal(11), "impressions": Decimal(100),
                      "ctr": Decimal("0.1"), "position": Decimal("3")}}
    report = compare_dimension_rows("gsc_pages", a, b, label_a="rn", label_b="ga")
    assert not report.passed
    assert [f.metric for f in report.failures] == ["clicks"]


def test_dimension_rows_key_only_on_one_side_fails_gate() -> None:
    a = {"/blog/a": {"clicks": Decimal(1), "impressions": Decimal(1),
                      "ctr": Decimal("1"), "position": Decimal("1")}}
    b = {
        "/blog/a": {"clicks": Decimal(1), "impressions": Decimal(1),
                     "ctr": Decimal("1"), "position": Decimal("1")},
        "/blog/b": {"clicks": Decimal(1), "impressions": Decimal(1),
                     "ctr": Decimal("1"), "position": Decimal("1")},
    }
    report = compare_dimension_rows("gsc_pages", a, b, label_a="rn", label_b="ga")
    assert report.keys_only_b == ["/blog/b"]
    assert not report.passed


def test_dimension_rows_ga4_breakdown_relative_tolerance() -> None:
    a = {"Organic Search": {"users": Decimal(1000), "sessions": Decimal(1200),
                             "conversions": Decimal(10), "engagement_rate": Decimal(70)}}
    b = {"Organic Search": {"users": Decimal(1005), "sessions": Decimal(1200),
                             "conversions": Decimal(10), "engagement_rate": Decimal(70)}}
    report = compare_dimension_rows("ga4_channels", a, b, label_a="rn", label_b="ga")
    assert report.passed  # 0.5% < 1% tolerance


def test_dimension_rows_kpi_aggregate_single_synthetic_key() -> None:
    a = {"total": {"clicks": Decimal(1736), "impressions": Decimal(281736),
                    "avg_position": Decimal("10.596")}}
    b = {"total": {"clicks": Decimal(1736), "impressions": Decimal(281736),
                    "avg_position": Decimal("10.600")}}
    report = compare_dimension_rows("gsc_kpi", a, b, label_a="rn", label_b="ga")
    assert report.passed


def test_dimension_rows_none_on_one_side_only_is_a_failure() -> None:
    a = {"purchase": {"events": Decimal(5), "conversions": None, "users": Decimal(3)}}
    b = {"purchase": {"events": Decimal(5), "conversions": Decimal(2), "users": Decimal(3)}}
    report = compare_dimension_rows("ga4_events", a, b, label_a="rn", label_b="ga")
    assert not report.passed
    assert report.failures[0].metric == "conversions"
    assert "only in" in report.failures[0].note


# ── task-89 §7.1 regression: both sides exactly 0 on a relative metric ─────
def test_both_zero_relative_metric_is_a_pass_not_a_failure() -> None:
    """`ctr == 0` on both sides (a real page with 0 clicks and 0 impressions
    in the window) must not fail the gate — found live on HWELL, where this
    silently failed ~two thirds of a 255-page comparison before the fix."""
    a = {"/low-traffic": {"clicks": Decimal(0), "impressions": Decimal(0),
                           "ctr": Decimal(0), "position": Decimal(0)}}
    b = {"/low-traffic": {"clicks": Decimal(0), "impressions": Decimal(0),
                           "ctr": Decimal(0), "position": Decimal(0)}}
    report = compare_dimension_rows("gsc_pages", a, b, label_a="rn", label_b="ga")
    assert report.passed
    ctr_summary = next(m for m in report.per_metric if m.metric == "ctr")
    assert ctr_summary.failures == 0


def test_both_zero_relative_metric_passes_in_date_series_too() -> None:
    """Same fix, exercised through `compare_series` (task-83's original
    date-keyed gate shares `_rel_pct` with `compare_dimension_rows`)."""
    a = _series(**{"2026-08-01": {"sessions": Decimal(0), "users": Decimal(0),
                                  "conversions": Decimal(0), "engagement_rate": Decimal(0)}})
    b = _series(**{"2026-08-01": {"sessions": Decimal(0), "users": Decimal(0),
                                  "conversions": Decimal(0), "engagement_rate": Decimal(0)}})
    report = compare_series("ga4_daily", a, b, label_a="rn", label_b="ga", lag_days=2, today=TODAY)
    assert report.passed
