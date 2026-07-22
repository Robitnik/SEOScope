from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from app.metrics.periods import resolve_periods
from app.schemas.enums import ComparisonMode, PeriodKind

UTC = ZoneInfo("UTC")
NY = ZoneInfo("America/New_York")


def _utc(*args) -> datetime:
    return datetime(*args, tzinfo=UTC)


# --- closed week --------------------------------------------------------


def test_closed_week_matches_plan_example():
    # PLAN §4.1 worked example: "13–19 Jul vs 6–12 Jul" as of Wed 2026-07-22.
    periods = resolve_periods(PeriodKind.week, _utc(2026, 7, 22, 12, 0), tz=NY.key)

    assert periods.current.start == date(2026, 7, 13)
    assert periods.current.end == date(2026, 7, 19)
    assert periods.previous.start == date(2026, 7, 6)
    assert periods.previous.end == date(2026, 7, 12)
    assert periods.label == "13–19 липня vs 6–12 липня"


def test_closed_week_crosses_year_boundary():
    periods = resolve_periods(PeriodKind.week, _utc(2027, 1, 5, 12, 0), tz=NY.key)

    assert periods.current.start == date(2026, 12, 28)
    assert periods.current.end == date(2027, 1, 3)
    assert periods.previous.start == date(2026, 12, 21)
    assert periods.previous.end == date(2026, 12, 27)
    assert periods.label == "28 грудня 2026 – 3 січня 2027 vs 21–27 грудня"


# --- closed month --------------------------------------------------------


def test_closed_month_basic():
    periods = resolve_periods(PeriodKind.month, _utc(2026, 7, 22, 12, 0), tz=NY.key)

    assert periods.current.start == date(2026, 6, 1)
    assert periods.current.end == date(2026, 6, 30)
    assert periods.previous.start == date(2026, 5, 1)
    assert periods.previous.end == date(2026, 5, 31)


def test_closed_month_leap_year_february():
    periods = resolve_periods(PeriodKind.month, _utc(2028, 3, 1, 12, 0), tz=NY.key)

    assert periods.current.start == date(2028, 2, 1)
    assert periods.current.end == date(2028, 2, 29)  # 2028 is a leap year
    assert periods.previous.start == date(2028, 1, 1)
    assert periods.previous.end == date(2028, 1, 31)


def test_closed_month_non_leap_year_february():
    periods = resolve_periods(PeriodKind.month, _utc(2027, 3, 1, 12, 0), tz=NY.key)

    assert periods.current.end == date(2027, 2, 28)


def test_closed_month_crosses_year_boundary():
    periods = resolve_periods(PeriodKind.month, _utc(2027, 1, 1, 12, 0), tz=NY.key)

    assert periods.current.start == date(2026, 12, 1)
    assert periods.current.end == date(2026, 12, 31)
    assert periods.previous.start == date(2026, 11, 1)
    assert periods.previous.end == date(2026, 11, 30)


# --- timezone / DST --------------------------------------------------------


def test_timezone_conversion_shifts_local_date_before_utc_midnight():
    # 02:00 UTC on 2026-07-23 is still 22:00 the previous day in New York
    # (EDT, UTC-4) — the anchor date must resolve against local time.
    periods = resolve_periods(PeriodKind.week, _utc(2026, 7, 23, 2, 0), tz=NY.key)

    assert periods.current.end == date(2026, 7, 19)


def test_dst_spring_forward_boundary_resolves_correct_local_date():
    # US DST spring-forward for 2026 is 2026-03-08 02:00 local (EST -> EDT).
    # 04:30 UTC on 2026-03-08 is 23:30 the previous day (Sat) in New York;
    # the same wall-clock instant a day later is already 00:30 EDT (Mon).
    before = resolve_periods(PeriodKind.week, _utc(2026, 3, 8, 4, 30), tz=NY.key)
    after = resolve_periods(PeriodKind.week, _utc(2026, 3, 9, 4, 30), tz=NY.key)

    assert before.current.end == date(2026, 3, 1)  # anchor local date 2026-03-07 (Sat)
    assert after.current.end == date(2026, 3, 8)  # anchor local date 2026-03-09 (Mon)


def test_naive_anchor_rejected():
    with pytest.raises(ValueError):
        resolve_periods(PeriodKind.week, datetime(2026, 7, 22, 12, 0), tz=NY.key)


# --- to_date ---------------------------------------------------------------


def test_week_to_date_previous_truncated_to_same_elapsed_days():
    periods = resolve_periods(
        PeriodKind.week, _utc(2026, 7, 22, 12, 0), tz=NY.key, mode=ComparisonMode.to_date
    )

    assert periods.current.start == date(2026, 7, 20)
    assert periods.current.end == date(2026, 7, 22)  # 3 elapsed days
    assert periods.previous.start == date(2026, 7, 13)
    assert periods.previous.end == date(2026, 7, 15)  # also 3 days


def test_month_to_date_previous_capped_at_shorter_february():
    # 2026-03-31 to_date: elapsed 31 days, but February 2026 only has 28.
    periods = resolve_periods(
        PeriodKind.month, _utc(2026, 3, 31, 12, 0), tz=NY.key, mode=ComparisonMode.to_date
    )

    assert periods.current.start == date(2026, 3, 1)
    assert periods.current.end == date(2026, 3, 31)
    assert periods.previous.start == date(2026, 2, 1)
    assert periods.previous.end == date(2026, 2, 28)  # capped, not 2026-03-03


def test_month_to_date_uncapped_case():
    periods = resolve_periods(
        PeriodKind.month, _utc(2026, 3, 15, 12, 0), tz=NY.key, mode=ComparisonMode.to_date
    )

    assert periods.current.end == date(2026, 3, 15)  # 15 elapsed days
    assert periods.previous.start == date(2026, 2, 1)
    assert periods.previous.end == date(2026, 2, 15)
