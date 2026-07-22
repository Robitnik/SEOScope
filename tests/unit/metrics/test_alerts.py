"""Alerts engine (task-17, PLAN §21.5): declining/rising page detection.

The tests that matter most are the ones a naive implementation gets wrong:
percentage-only sorting, infinite growth from a `0` base, microvolume noise,
and building an alert off a `partial`/`truncated` period.
"""

from __future__ import annotations

from decimal import Decimal

from app.metrics.alerts import (
    AlertThresholds,
    DimensionRow,
    detect_alerts,
)
from app.schemas.enums import Quality


def _row(
    value: str,
    *,
    clicks: int | None,
    impressions: int | None = 1000,
    quality: Quality = Quality.complete,
    rank: int | None = 1,
) -> DimensionRow:
    return DimensionRow(
        dimension_value=value,
        clicks=clicks,
        impressions=impressions,
        quality=quality,
        rank=rank,
    )


# --- microvolume noise -------------------------------------------------


def test_micro_volume_move_does_not_create_an_alert():
    current = [_row("/a", clicks=2, impressions=5)]
    previous = [_row("/a", clicks=1, impressions=5)]

    result = detect_alerts(current, previous)

    assert result.declining == []
    assert result.rising == []
    assert result.skipped_reason is None


def test_base_volume_gate_passes_on_impressions_alone():
    # 10 -> 12 clicks is only 20%, right at the threshold, but the base
    # period's impressions (200) alone clear the noise floor.
    current = [_row("/a", clicks=12, impressions=250)]
    previous = [_row("/a", clicks=10, impressions=200)]

    result = detect_alerts(current, previous)

    assert len(result.rising) == 1
    assert result.rising[0].dimension_value == "/a"


# --- absolute-impact sorting, not percentage ----------------------------


def test_sorted_by_absolute_impact_not_percentage():
    current = [
        _row("/big", clicks=4000, impressions=10_000),
        _row("/small", clicks=10, impressions=100),
    ]
    previous = [
        _row("/big", clicks=5000, impressions=10_000),
        _row("/small", clicks=20, impressions=100),
    ]

    result = detect_alerts(current, previous)

    # Both drop >=20% (20% and 50% respectively), but -1000 outranks -10.
    assert [e.dimension_value for e in result.declining] == ["/big", "/small"]


# --- new pages -----------------------------------------------------------


def test_page_absent_from_previous_period_is_new_not_infinite_growth():
    current = [_row("/fresh", clicks=80, impressions=500)]
    previous: list[DimensionRow] = [_row("/other", clicks=100, impressions=1000)]

    result = detect_alerts(current, previous)

    assert result.declining == []
    assert result.rising == []
    assert [e.dimension_value for e in result.new] == ["/fresh"]
    assert result.new[0].delta_pct is None
    assert result.new[0].clicks_previous is None


def test_new_page_below_volume_floor_is_not_reported():
    current = [_row("/fresh", clicks=1, impressions=2)]
    previous: list[DimensionRow] = [_row("/other", clicks=100, impressions=1000)]

    result = detect_alerts(current, previous)

    assert result.new == []


# --- division by zero / None never produces inf or a crash ---------------


def test_zero_base_clicks_never_produces_infinite_percent():
    current = [_row("/a", clicks=5, impressions=500)]
    previous = [_row("/a", clicks=0, impressions=500)]

    result = detect_alerts(current, previous)

    assert len(result.rising) == 1
    assert result.rising[0].delta_pct is None
    assert result.rising[0].delta_abs == 5


def test_none_clicks_row_is_skipped_not_crashed():
    current = [_row("/a", clicks=None, impressions=500)]
    previous = [_row("/a", clicks=200, impressions=500)]

    result = detect_alerts(current, previous)

    assert result.declining == []
    assert result.rising == []
    assert result.new == []


# --- top-N remainder row excluded ----------------------------------------


def test_remainder_only_periods_produce_no_alerts():
    current = [_row("інше", clicks=50, impressions=5000, rank=None)]
    previous = [_row("інше", clicks=5000, impressions=50000, rank=None)]

    result = detect_alerts(current, previous)

    assert result.skipped_reason is None
    assert result.declining == []
    assert result.rising == []
    assert result.new == []


def test_remainder_row_excluded_even_when_quality_gate_passes():
    current = [
        _row("/a", clicks=100, impressions=1000, rank=1),
        _row("інше", clicks=50, impressions=5000, rank=None),
    ]
    previous = [
        _row("/a", clicks=100, impressions=1000, rank=1),
        _row("інше", clicks=5000, impressions=50000, rank=None),
    ]

    result = detect_alerts(current, previous)

    assert result.skipped_reason is None
    assert result.declining == []
    assert result.rising == []
    assert result.new == []


# --- quality gate: partial/truncated periods build no alerts -------------


def test_partial_current_period_skips_with_reason():
    current = [_row("/a", clicks=100, impressions=1000, quality=Quality.partial)]
    previous = [_row("/a", clicks=200, impressions=1000)]

    result = detect_alerts(current, previous)

    assert result.declining == []
    assert result.rising == []
    assert result.new == []
    assert result.skipped_reason == "current_period_partial"


def test_truncated_previous_period_skips_with_reason():
    current = [_row("/a", clicks=100, impressions=1000)]
    previous = [_row("/a", clicks=200, impressions=1000, quality=Quality.truncated)]

    result = detect_alerts(current, previous)

    assert result.skipped_reason == "previous_period_truncated"


def test_missing_period_data_skips_with_reason():
    result = detect_alerts([], [_row("/a", clicks=200, impressions=1000)])

    assert result.skipped_reason == "missing_period_data"


# --- thresholds are configurable, not hardcoded ---------------------------


def test_custom_relative_change_threshold_changes_result():
    current = [_row("/a", clicks=91, impressions=1000)]
    previous = [_row("/a", clicks=100, impressions=1000)]  # -9%

    default = detect_alerts(current, previous)
    assert default.declining == []

    loose = detect_alerts(current, previous, AlertThresholds(min_relative_change_pct=Decimal("5")))
    assert len(loose.declining) == 1


def test_custom_base_volume_threshold_changes_result():
    current = [_row("/a", clicks=2, impressions=30)]
    previous = [_row("/a", clicks=1, impressions=30)]  # below default 50/10 floor

    default = detect_alerts(current, previous)
    assert default.rising == []

    loose = detect_alerts(
        current, previous, AlertThresholds(min_base_impressions=10, min_base_clicks=1)
    )
    assert len(loose.rising) == 1


def test_top_n_per_group_limits_result_size():
    current = [_row(f"/p{i}", clicks=200, impressions=1000, rank=i + 1) for i in range(5)]
    previous = [_row(f"/p{i}", clicks=100, impressions=1000, rank=i + 1) for i in range(5)]

    result = detect_alerts(current, previous, AlertThresholds(top_n_per_group=2))

    assert len(result.rising) == 2
