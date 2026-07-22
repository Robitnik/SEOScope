"""Extended periods: quarter / year / custom (task-14, PLAN §21.1).

Week/month behaviour is covered by `test_periods.py` and must not change —
`PeriodKind` was extended additively.
"""

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from app.metrics.periods import resolve_periods
from app.schemas.enums import ComparisonMode, PeriodKind

UTC = ZoneInfo("UTC")
NY = ZoneInfo("America/New_York")


def _utc(*args) -> datetime:
    return datetime(*args, tzinfo=UTC)


# --- closed quarter ------------------------------------------------------


def test_closed_quarter_is_last_full_quarter():
    # 2026-07-22 sits in Q3, so the last full quarter is Q2 vs Q1.
    periods = resolve_periods(PeriodKind.quarter, _utc(2026, 7, 22, 12, 0), tz=NY.key)

    assert periods.current.start == date(2026, 4, 1)
    assert periods.current.end == date(2026, 6, 30)
    assert periods.previous.start == date(2026, 1, 1)
    assert periods.previous.end == date(2026, 3, 31)
    assert periods.label == "Q2 2026 vs Q1 2026"


def test_closed_quarter_crosses_year_boundary():
    periods = resolve_periods(PeriodKind.quarter, _utc(2026, 2, 10, 12, 0), tz=NY.key)

    assert periods.current.start == date(2025, 10, 1)
    assert periods.current.end == date(2025, 12, 31)
    assert periods.previous.start == date(2025, 7, 1)
    assert periods.previous.end == date(2025, 9, 30)
    assert periods.label == "Q4 2025 vs Q3 2025"


def test_closed_quarter_previous_covers_leap_day():
    # Q1 2028 is a leap quarter (91 days) compared against a 92-day Q2 —
    # calendar quarters are not equal-length and must not be normalized.
    periods = resolve_periods(PeriodKind.quarter, _utc(2028, 8, 1, 12, 0), tz=NY.key)

    assert periods.current.start == date(2028, 4, 1)
    assert periods.current.end == date(2028, 6, 30)
    assert periods.previous.start == date(2028, 1, 1)
    assert periods.previous.end == date(2028, 3, 31)
    assert (periods.current.end - periods.current.start).days + 1 == 91
    assert (periods.previous.end - periods.previous.start).days + 1 == 91  # leap Feb


def test_quarter_boundary_days_pick_the_right_quarter():
    first_day_of_q3 = resolve_periods(PeriodKind.quarter, _utc(2026, 7, 1, 4, 0), tz=NY.key)
    last_day_of_q2 = resolve_periods(PeriodKind.quarter, _utc(2026, 6, 30, 12, 0), tz=NY.key)

    assert first_day_of_q3.current.end == date(2026, 6, 30)
    # On the last day of Q2 the quarter is not finished, so Q1 is the last full one.
    assert last_day_of_q2.current.start == date(2026, 1, 1)
    assert last_day_of_q2.current.end == date(2026, 3, 31)


# --- closed year ---------------------------------------------------------


def test_closed_year_is_last_full_year():
    periods = resolve_periods(PeriodKind.year, _utc(2026, 7, 22, 12, 0), tz=NY.key)

    assert periods.current.start == date(2025, 1, 1)
    assert periods.current.end == date(2025, 12, 31)
    assert periods.previous.start == date(2024, 1, 1)
    assert periods.previous.end == date(2024, 12, 31)
    assert periods.label == "2025 vs 2024"


def test_closed_year_previous_keeps_its_leap_day():
    periods = resolve_periods(PeriodKind.year, _utc(2026, 1, 1, 12, 0), tz=NY.key)

    assert (periods.previous.end - periods.previous.start).days + 1 == 366  # 2024
    assert (periods.current.end - periods.current.start).days + 1 == 365  # 2025


def test_year_uses_local_calendar_date_not_utc():
    # 2027-01-01 00:30 UTC is still 2026-12-31 in New York.
    periods = resolve_periods(PeriodKind.year, _utc(2027, 1, 1, 0, 30), tz=NY.key)

    assert periods.current.start == date(2025, 1, 1)


# --- custom --------------------------------------------------------------


def test_custom_previous_has_the_same_length_and_ends_the_day_before():
    periods = resolve_periods(
        PeriodKind.custom,
        _utc(2026, 7, 22, 12, 0),
        tz=NY.key,
        custom_start=date(2026, 4, 1),
        custom_end=date(2026, 5, 15),
    )

    length = (periods.current.end - periods.current.start).days + 1
    assert length == 45
    assert periods.previous.end == date(2026, 3, 31)
    assert periods.previous.start == date(2026, 2, 15)
    assert (periods.previous.end - periods.previous.start).days + 1 == length
    assert periods.label == "Apr 1 – May 15 2026 vs Feb 15 – Mar 31 2026"


def test_custom_odd_length_range_of_one_day():
    periods = resolve_periods(
        PeriodKind.custom,
        _utc(2026, 7, 22, 12, 0),
        custom_start=date(2026, 3, 1),
        custom_end=date(2026, 3, 1),
    )

    assert periods.current.start == periods.current.end == date(2026, 3, 1)
    assert periods.previous.start == periods.previous.end == date(2026, 2, 28)
    assert periods.label == "Mar 1–1 2026 vs Feb 28–28 2026"


def test_custom_previous_may_cross_a_year_boundary():
    periods = resolve_periods(
        PeriodKind.custom,
        _utc(2026, 7, 22, 12, 0),
        custom_start=date(2026, 1, 10),
        custom_end=date(2026, 2, 8),
    )

    assert periods.previous.start == date(2025, 12, 11)
    assert periods.previous.end == date(2026, 1, 9)
    assert periods.label == "Jan 10 – Feb 8 2026 vs Dec 11 2025 – Jan 9 2026"


def test_custom_ignores_the_anchor():
    kwargs = dict(custom_start=date(2026, 4, 1), custom_end=date(2026, 4, 30))
    early = resolve_periods(PeriodKind.custom, _utc(2026, 5, 1, 12, 0), **kwargs)
    late = resolve_periods(PeriodKind.custom, _utc(2030, 5, 1, 12, 0), **kwargs)

    assert early.current == late.current
    assert early.previous == late.previous


def test_custom_without_bounds_is_rejected():
    with pytest.raises(ValueError, match="custom_start and custom_end"):
        resolve_periods(PeriodKind.custom, _utc(2026, 7, 22, 12, 0))


def test_custom_with_inverted_bounds_is_rejected():
    with pytest.raises(ValueError, match="precedes"):
        resolve_periods(
            PeriodKind.custom,
            _utc(2026, 7, 22, 12, 0),
            custom_start=date(2026, 5, 1),
            custom_end=date(2026, 4, 1),
        )


# --- to_date mode --------------------------------------------------------


def test_quarter_to_date_compares_equal_elapsed_days():
    periods = resolve_periods(
        PeriodKind.quarter,
        _utc(2026, 8, 10, 12, 0),
        tz=NY.key,
        mode=ComparisonMode.to_date,
    )

    assert periods.current.start == date(2026, 7, 1)
    assert periods.current.end == date(2026, 8, 10)
    elapsed = (periods.current.end - periods.current.start).days + 1
    assert periods.previous.start == date(2026, 4, 1)
    assert (periods.previous.end - periods.previous.start).days + 1 == elapsed
    # A partial quarter must not be labelled "Q2 2026" — that would claim a
    # full quarter we do not have.
    assert periods.label == "Jul 1 – Aug 10 2026 vs Apr 1 – May 11 2026"


def test_year_to_date_clamps_previous_to_the_year_end():
    periods = resolve_periods(
        PeriodKind.year,
        _utc(2026, 12, 31, 12, 0),
        tz=NY.key,
        mode=ComparisonMode.to_date,
    )

    assert periods.current.start == date(2026, 1, 1)
    assert periods.current.end == date(2026, 12, 31)
    assert periods.previous.start == date(2025, 1, 1)
    assert periods.previous.end == date(2025, 12, 31)


def test_year_to_date_previous_is_clamped_when_current_is_a_leap_year():
    # 2028 is a leap year: day 366 of 2028 has no counterpart in 2027.
    periods = resolve_periods(
        PeriodKind.year,
        _utc(2028, 12, 31, 12, 0),
        tz=NY.key,
        mode=ComparisonMode.to_date,
    )

    assert (periods.current.end - periods.current.start).days + 1 == 366
    assert periods.previous.end == date(2027, 12, 31)
    assert (periods.previous.end - periods.previous.start).days + 1 == 365
