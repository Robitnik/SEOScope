"""Period engine (PLAN §3.1, §21.1): exact ranges for every `PeriodKind`.

Pure calendar-date arithmetic. The only place a `datetime` is involved is
converting the caller-supplied `anchor` into a local calendar `date` via
`tz` — everything after that is `date` math, so leap years, month-length
differences and DST are non-issues (a `date` has no time-of-day/offset).

`closed` mode (default) compares the last full week/month/quarter/year
against the one before it — both periods always have equal length *for the
same calendar unit* (a 92-day quarter is compared against a 90-day one:
that is the calendar, not a bug). `to_date` mode compares "so far this
period" against the same number of elapsed days in the previous period; it
exists per PLAN §3.1 but is flagged as not yet wired into the UI pending a
separate UX decision.

`custom` (task-14) is the one kind that ignores `anchor`: the caller passes
`custom_start`/`custom_end` explicitly and `previous` is the range of the
**same length** immediately preceding it. Extended kinds carry no data of
their own — they are served by aggregating stored history
(`app.metrics.aggregate`), never by a new collection (§21.1).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict

from app.schemas.adapter import DateRange
from app.schemas.enums import ComparisonMode, PeriodKind

# Month names, as used in "Jul 13-19" (PLAN §3.1 originally specified
# Ukrainian genitive-case names; translated to English per explicit request —
# all frontend-facing text must be English).
_MONTH_NAMES: tuple[str, ...] = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)

# Abbreviated month names for the compact `custom`/`to_date` label form from
# PLAN §21.1 ("Apr 1 - May 15 2026"): arbitrary ranges can span months and
# years, so the year is always spelled out and the month name is shortened
# to keep the label one line.
_MONTH_ABBR: tuple[str, ...] = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)


class Periods(BaseModel):
    """Result of `resolve_periods`: exact current/previous ranges + UI label."""

    model_config = ConfigDict(extra="forbid")

    current: DateRange
    previous: DateRange
    label: str
    # Not computed here — providers with data lag (e.g. GSC) fill this in
    # after collection; the period engine has no I/O and cannot know it.
    data_through_hint: date | None = None


def _to_local_date(anchor: datetime, tz: str) -> date:
    if anchor.tzinfo is None:
        raise ValueError("resolve_periods requires a timezone-aware anchor datetime")
    return anchor.astimezone(ZoneInfo(tz)).date()


def _month_label(month: int) -> str:
    return _MONTH_NAMES[month - 1]


def _format_range(dr: DateRange) -> str:
    start, end = dr.start, dr.end
    if start.year == end.year and start.month == end.month:
        return f"{_month_label(start.month)} {start.day}–{end.day}"
    if start.year == end.year:
        return f"{_month_label(start.month)} {start.day} – {_month_label(end.month)} {end.day}"
    return (
        f"{_month_label(start.month)} {start.day} {start.year} – "
        f"{_month_label(end.month)} {end.day} {end.year}"
    )


def _format_compact_range(dr: DateRange) -> str:
    """Compact, year-explicit label for arbitrary ranges (PLAN §21.1)."""
    start, end = dr.start, dr.end
    start_month, end_month = _MONTH_ABBR[start.month - 1], _MONTH_ABBR[end.month - 1]
    if start.year == end.year:
        if start.month == end.month:
            return f"{start_month} {start.day}–{end.day} {start.year}"
        return f"{start_month} {start.day} – {end_month} {end.day} {start.year}"
    return f"{start_month} {start.day} {start.year} – {end_month} {end.day} {end.year}"


def _quarter_index(d: date) -> int:
    """0-based calendar quarter of `d`."""
    return (d.month - 1) // 3


def _quarter_start(d: date) -> date:
    return date(d.year, _quarter_index(d) * 3 + 1, 1)


def _shift_quarter(quarter_start: date, quarters: int) -> date:
    """Start of the quarter `quarters` away from `quarter_start` (may be negative)."""
    months = quarter_start.year * 12 + (quarter_start.month - 1) + quarters * 3
    return date(months // 12, months % 12 + 1, 1)


def _quarter_label(dr: DateRange) -> str:
    return f"Q{_quarter_index(dr.start) + 1} {dr.start.year}"


def _year_label(dr: DateRange) -> str:
    return str(dr.start.year)


def _last_full_week_closed(anchor_date: date) -> tuple[DateRange, DateRange]:
    this_week_start = anchor_date - timedelta(days=anchor_date.weekday())
    current_end = this_week_start - timedelta(days=1)
    current_start = current_end - timedelta(days=6)
    previous_end = current_start - timedelta(days=1)
    previous_start = previous_end - timedelta(days=6)
    return (
        DateRange(start=current_start, end=current_end),
        DateRange(start=previous_start, end=previous_end),
    )


def _last_full_month_closed(anchor_date: date) -> tuple[DateRange, DateRange]:
    this_month_start = anchor_date.replace(day=1)
    current_end = this_month_start - timedelta(days=1)
    current_start = current_end.replace(day=1)
    previous_end = current_start - timedelta(days=1)
    previous_start = previous_end.replace(day=1)
    return (
        DateRange(start=current_start, end=current_end),
        DateRange(start=previous_start, end=previous_end),
    )


def _last_full_quarter_closed(anchor_date: date) -> tuple[DateRange, DateRange]:
    this_quarter_start = _quarter_start(anchor_date)
    current_start = _shift_quarter(this_quarter_start, -1)
    current_end = this_quarter_start - timedelta(days=1)
    previous_start = _shift_quarter(this_quarter_start, -2)
    previous_end = current_start - timedelta(days=1)
    return (
        DateRange(start=current_start, end=current_end),
        DateRange(start=previous_start, end=previous_end),
    )


def _last_full_year_closed(anchor_date: date) -> tuple[DateRange, DateRange]:
    return (
        DateRange(start=date(anchor_date.year - 1, 1, 1), end=date(anchor_date.year - 1, 12, 31)),
        DateRange(start=date(anchor_date.year - 2, 1, 1), end=date(anchor_date.year - 2, 12, 31)),
    )


def _week_to_date(anchor_date: date) -> tuple[DateRange, DateRange]:
    current_start = anchor_date - timedelta(days=anchor_date.weekday())
    current_end = anchor_date
    elapsed_days = (current_end - current_start).days + 1
    previous_start = current_start - timedelta(weeks=1)
    previous_end = previous_start + timedelta(days=elapsed_days - 1)
    return (
        DateRange(start=current_start, end=current_end),
        DateRange(start=previous_start, end=previous_end),
    )


def _month_to_date(anchor_date: date) -> tuple[DateRange, DateRange]:
    current_start = anchor_date.replace(day=1)
    current_end = anchor_date
    elapsed_days = (current_end - current_start).days + 1
    prev_month_last_day = current_start - timedelta(days=1)
    previous_start = prev_month_last_day.replace(day=1)
    previous_end = min(previous_start + timedelta(days=elapsed_days - 1), prev_month_last_day)
    return (
        DateRange(start=current_start, end=current_end),
        DateRange(start=previous_start, end=previous_end),
    )


def _quarter_to_date(anchor_date: date) -> tuple[DateRange, DateRange]:
    current_start = _quarter_start(anchor_date)
    current_end = anchor_date
    elapsed_days = (current_end - current_start).days + 1
    previous_start = _shift_quarter(current_start, -1)
    previous_quarter_end = current_start - timedelta(days=1)
    previous_end = min(previous_start + timedelta(days=elapsed_days - 1), previous_quarter_end)
    return (
        DateRange(start=current_start, end=current_end),
        DateRange(start=previous_start, end=previous_end),
    )


def _year_to_date(anchor_date: date) -> tuple[DateRange, DateRange]:
    current_start = date(anchor_date.year, 1, 1)
    current_end = anchor_date
    elapsed_days = (current_end - current_start).days + 1
    previous_start = date(anchor_date.year - 1, 1, 1)
    previous_year_end = date(anchor_date.year - 1, 12, 31)
    previous_end = min(previous_start + timedelta(days=elapsed_days - 1), previous_year_end)
    return (
        DateRange(start=current_start, end=current_end),
        DateRange(start=previous_start, end=previous_end),
    )


def _custom_range(
    custom_start: date | None, custom_end: date | None
) -> tuple[DateRange, DateRange]:
    """Arbitrary current range + the equally long range right before it."""
    if custom_start is None or custom_end is None:
        raise ValueError("PeriodKind.custom requires both custom_start and custom_end")
    if custom_end < custom_start:
        raise ValueError(f"custom_end {custom_end} precedes custom_start {custom_start}")
    length_days = (custom_end - custom_start).days + 1
    previous_end = custom_start - timedelta(days=1)
    previous_start = previous_end - timedelta(days=length_days - 1)
    return (
        DateRange(start=custom_start, end=custom_end),
        DateRange(start=previous_start, end=previous_end),
    )


def resolve_periods(
    kind: PeriodKind,
    anchor: datetime,
    tz: str = "America/New_York",
    mode: ComparisonMode = ComparisonMode.closed,
    *,
    custom_start: date | None = None,
    custom_end: date | None = None,
) -> Periods:
    """Resolve exact current/previous `DateRange`s for `kind` as of `anchor`.

    `anchor` must be a timezone-aware datetime (typically "now" in UTC); it
    is converted to a local calendar date in `tz` before any range math.

    `custom_start`/`custom_end` are required by — and only used for —
    `PeriodKind.custom`, which ignores both `anchor` and `mode` because the
    caller has already stated the exact range (PLAN §21.1).
    """
    anchor_date = _to_local_date(anchor, tz)

    if kind == PeriodKind.custom:
        current, previous = _custom_range(custom_start, custom_end)
    elif mode == ComparisonMode.closed:
        if kind == PeriodKind.week:
            current, previous = _last_full_week_closed(anchor_date)
        elif kind == PeriodKind.month:
            current, previous = _last_full_month_closed(anchor_date)
        elif kind == PeriodKind.quarter:
            current, previous = _last_full_quarter_closed(anchor_date)
        else:
            current, previous = _last_full_year_closed(anchor_date)
    else:
        if kind == PeriodKind.week:
            current, previous = _week_to_date(anchor_date)
        elif kind == PeriodKind.month:
            current, previous = _month_to_date(anchor_date)
        elif kind == PeriodKind.quarter:
            current, previous = _quarter_to_date(anchor_date)
        else:
            current, previous = _year_to_date(anchor_date)

    label = f"{_label_for(kind, mode, current)} vs {_label_for(kind, mode, previous)}"
    return Periods(current=current, previous=previous, label=label)


def _label_for(kind: PeriodKind, mode: ComparisonMode, dr: DateRange) -> str:
    """Human label for one side of the comparison.

    The `Q3 2026` / `2025` shorthands are only used when the range really is
    the whole calendar unit — in `to_date` mode they would claim a full
    quarter/year we do not have, so those fall back to explicit dates.
    """
    if mode == ComparisonMode.closed:
        if kind == PeriodKind.quarter:
            return _quarter_label(dr)
        if kind == PeriodKind.year:
            return _year_label(dr)
    if kind in (PeriodKind.week, PeriodKind.month):
        return _format_range(dr)
    return _format_compact_range(dr)
