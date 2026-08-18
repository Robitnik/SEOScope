from __future__ import annotations

from datetime import date

import pytest

from app.reports.periods import (
    InvalidComparatorMonthsError,
    InvalidPeriodError,
    build_comparator_periods,
    build_custom_months_periods,
    build_monthly_periods,
    build_yearly_periods,
)


def test_monthly_periods_match_the_worked_example_from_the_task_doc():
    # task-42 §3.1: today = Jun 19, 2026 -> ref = May 31, 2026.
    periods = build_monthly_periods(today=date(2026, 6, 19))
    by_label = {p.label: p for p in periods}

    assert by_label["This Month"].date_from == date(2026, 5, 1)
    assert by_label["This Month"].date_to == date(2026, 5, 31)
    assert by_label["Last Month"].date_from == date(2026, 4, 1)
    assert by_label["Last Month"].date_to == date(2026, 4, 30)
    assert by_label["Month YOY"].date_from == date(2025, 5, 1)
    assert by_label["Month YOY"].date_to == date(2025, 5, 31)
    assert by_label["This Quarter"].date_from == date(2026, 3, 1)
    assert by_label["This Quarter"].date_to == date(2026, 5, 31)
    assert by_label["Last Quarter"].date_from == date(2025, 12, 1)
    assert by_label["Last Quarter"].date_to == date(2026, 2, 28)
    assert by_label["Quarterly YOY"].date_from == date(2025, 3, 1)
    assert by_label["Quarterly YOY"].date_to == date(2025, 5, 31)


def test_monthly_periods_are_never_partial():
    periods = build_monthly_periods(today=date(2026, 6, 19))
    assert all(not p.is_partial for p in periods)
    assert [p.label for p in periods] == [
        "This Month",
        "Last Month",
        "Month YOY",
        "This Quarter",
        "Last Quarter",
        "Quarterly YOY",
    ]


def test_yearly_ytd_uses_the_same_window_every_year_and_is_never_partial():
    periods = build_yearly_periods([2025, 2026], "ytd", today=date(2026, 6, 19))
    assert [p.label for p in periods] == ["2026", "2025"]
    by_label = {p.label: p for p in periods}
    assert by_label["2026"].date_from == date(2026, 1, 1)
    assert by_label["2026"].date_to == date(2026, 5, 31)
    assert by_label["2025"].date_from == date(2025, 1, 1)
    assert by_label["2025"].date_to == date(2025, 5, 31)
    assert not any(p.is_partial for p in periods)


def test_yearly_full_clips_the_current_year_and_flags_it_partial():
    periods = build_yearly_periods([2024, 2025, 2026], "full", today=date(2026, 6, 19))
    by_label = {p.label: p for p in periods}
    assert by_label["2026"].date_to == date(2026, 5, 31)
    assert by_label["2026"].is_partial is True
    assert by_label["2025"].date_from == date(2025, 1, 1)
    assert by_label["2025"].date_to == date(2025, 12, 31)
    assert by_label["2025"].is_partial is False


def test_yearly_rejects_a_future_year():
    with pytest.raises(InvalidPeriodError):
        build_yearly_periods([2026, 2027], "full", today=date(2026, 6, 19))


@pytest.mark.parametrize("years", [[2026], [2023, 2024, 2025, 2026, 2027]])
def test_yearly_rejects_out_of_bounds_year_counts(years):
    with pytest.raises(InvalidPeriodError):
        build_yearly_periods(years, "ytd", today=date(2026, 6, 19))


def test_custom_months_orders_newest_first_regardless_of_input_order():
    periods = build_custom_months_periods(
        ["2025-01", "2026-03", "2025-12"], today=date(2026, 6, 19)
    )
    assert [p.label for p in periods] == ["March 2026", "December 2025", "January 2025"]


def test_custom_months_flags_the_current_month_partial_and_clips_to_today():
    periods = build_custom_months_periods(["2026-06"], today=date(2026, 6, 19))
    assert periods[0].date_from == date(2026, 6, 1)
    assert periods[0].date_to == date(2026, 6, 19)
    assert periods[0].is_partial is True


def test_custom_months_rejects_a_future_month():
    with pytest.raises(InvalidPeriodError):
        build_custom_months_periods(["2026-07"], today=date(2026, 6, 19))


def test_custom_months_rejects_out_of_bounds_counts():
    with pytest.raises(InvalidPeriodError):
        build_custom_months_periods([], today=date(2026, 6, 19))
    with pytest.raises(InvalidPeriodError):
        many_months = [f"2020-{m:02d}" for m in range(1, 13)] * 3
        build_custom_months_periods(many_months, today=date(2026, 6, 19))


def test_comparator_accepts_the_same_month_across_years():
    periods = build_comparator_periods(
        ["2026-07", "2025-07", "2024-07", "2023-07"], today=date(2026, 8, 17)
    )
    assert [p.label for p in periods] == ["July 2026", "July 2025", "July 2024", "July 2023"]
    assert all(p.date_from.day == 1 for p in periods)
    assert not any(p.is_partial for p in periods)


def test_comparator_rejects_mismatched_month_numbers():
    with pytest.raises(InvalidComparatorMonthsError):
        build_comparator_periods(["2026-07", "2025-08"], today=date(2026, 8, 17))


def test_comparator_rejects_out_of_bounds_counts():
    with pytest.raises(InvalidPeriodError):
        build_comparator_periods(["2026-07"], today=date(2026, 8, 17))
    with pytest.raises(InvalidPeriodError):
        build_comparator_periods(
            ["2026-07", "2025-07", "2024-07", "2023-07", "2022-07"], today=date(2026, 8, 17)
        )
