"""Pure transport-comparison logic (task-83 §7)."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from decimal import Decimal

from app.integrations.google_api.compare import compare_series

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
