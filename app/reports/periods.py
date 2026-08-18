"""Date-range builders for the 4 report kinds (task-42 §2.4/§3.1).

One engine, four builders: each returns a `list[ReportPeriod]` and the rest
of the engine (API path, PDF path, table, XLSX) never has to know which
`kind` produced it — "add a 5th kind" means adding one more builder here.

Ported from `AISeoTools/.../clientreport/periods.py` (monthly logic unchanged
— it already anchors to the last COMPLETE month, so none of its 6 columns
are ever partial) plus 3 new builders for yearly/custom_months/comparator.
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date, timedelta

_MONTH_RE = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])$")


@dataclass(frozen=True)
class ReportPeriod:
    label: str
    date_from: date
    date_to: date
    is_partial: bool = False


class InvalidPeriodError(ValueError):
    """A requested period is malformed or refers to the future (task-42 §5 `422 invalid_period`)."""


class InvalidComparatorMonthsError(ValueError):
    """Comparator months don't share a calendar month (`422 invalid_comparator_months`)."""


def _month_back(d: date, n: int) -> date:
    """First day of the month that is n months before the month of d."""
    year, month = d.year, d.month - n
    while month <= 0:
        month += 12
        year -= 1
    return date(year, month, 1)


def _month_last_day(d: date) -> date:
    return date(d.year, d.month, calendar.monthrange(d.year, d.month)[1])


def _parse_month(value: str) -> tuple[int, int]:
    match = _MONTH_RE.match(value)
    if not match:
        raise InvalidPeriodError(f"Not a valid YYYY-MM month: {value!r}")
    return int(match.group(1)), int(match.group(2))


def _month_bounds(year: int, month: int, today: date) -> tuple[date, date, bool]:
    """(date_from, date_to, is_partial) for one calendar month.

    A future month is rejected outright (task-42 §3.1 "місяць у майбутньому
    -> 422"); the current, still-open month is allowed but clipped to today
    and flagged `is_partial` — the UI must show that, never silently treat it
    as a closed month (task-42 §3.1/PLAN §12).
    """
    if (year, month) > (today.year, today.month):
        raise InvalidPeriodError(f"{year:04d}-{month:02d} is in the future")
    date_from = date(year, month, 1)
    if (year, month) == (today.year, today.month):
        return date_from, today, True
    return date_from, _month_last_day(date_from), False


def _month_label(year: int, month: int) -> str:
    return date(year, month, 1).strftime("%B %Y")


def build_monthly_periods(today: date | None = None) -> list[ReportPeriod]:
    """The 6 standard periods (task-42 §3.1 `kind=monthly`).

    All 6 are anchored to the last COMPLETE month, so none are ever partial
    — quarters are trailing 3-month blocks, not calendar quarters.
    """
    t = today or date.today()
    ref = date(t.year, t.month, 1) - timedelta(days=1)

    this_month_start = date(ref.year, ref.month, 1)
    this_month_end = ref

    last_month_end = this_month_start - timedelta(days=1)
    last_month_start = date(last_month_end.year, last_month_end.month, 1)

    month_yoy_start = date(this_month_start.year - 1, this_month_start.month, 1)
    month_yoy_end = _month_last_day(month_yoy_start)

    this_quarter_end = ref
    this_quarter_start = _month_back(ref, 2)

    last_quarter_end = this_quarter_start - timedelta(days=1)
    last_quarter_start = _month_back(last_quarter_end, 2)

    qyoy_start = date(this_quarter_start.year - 1, this_quarter_start.month, 1)
    qyoy_end = _month_last_day(date(this_quarter_end.year - 1, this_quarter_end.month, 1))

    return [
        ReportPeriod("This Month", this_month_start, this_month_end),
        ReportPeriod("Last Month", last_month_start, last_month_end),
        ReportPeriod("Month YOY", month_yoy_start, month_yoy_end),
        ReportPeriod("This Quarter", this_quarter_start, this_quarter_end),
        ReportPeriod("Last Quarter", last_quarter_start, last_quarter_end),
        ReportPeriod("Quarterly YOY", qyoy_start, qyoy_end),
    ]


def build_yearly_periods(
    years: list[int], span: str, *, today: date | None = None
) -> list[ReportPeriod]:
    """One column per year (task-42 §3.1 `kind=yearly`), newest first.

    `span="ytd"`: Jan 1 -> the last day of the last COMPLETE month, same
    window every year (apples to apples) — never flagged partial, since a
    YTD window is short by *definition*, not because live data is missing.
    `span="full"`: calendar year Jan 1 -> Dec 31, except the current year,
    clipped to the last complete month and flagged `is_partial` (task-42
    §3.1 explicit rule).
    """
    if span not in ("ytd", "full"):
        raise InvalidPeriodError(f"Unknown yearly span: {span!r}")
    if not 2 <= len(years) <= 4:
        raise InvalidPeriodError("yearly report needs 2-4 years")

    t = today or date.today()
    ref = date(t.year, t.month, 1) - timedelta(days=1)
    for year in years:
        if year > ref.year:
            raise InvalidPeriodError(f"{year} is in the future")

    periods: list[ReportPeriod] = []
    for year in sorted(set(years), reverse=True):
        date_from = date(year, 1, 1)
        if span == "ytd":
            date_to = _month_last_day(date(year, ref.month, 1))
            periods.append(ReportPeriod(str(year), date_from, date_to))
        else:
            is_current = year == ref.year
            date_to = ref if is_current else date(year, 12, 31)
            periods.append(ReportPeriod(str(year), date_from, date_to, is_partial=is_current))
    return periods


def build_custom_months_periods(
    months: list[str], *, today: date | None = None, max_months: int = 24
) -> list[ReportPeriod]:
    """One column per requested month (task-42 §3.1 `kind=custom_months`),
    newest first regardless of input order.
    """
    if not 1 <= len(months) <= max_months:
        raise InvalidPeriodError(f"custom report needs 1-{max_months} months")

    t = today or date.today()
    parsed = sorted({_parse_month(m) for m in months}, reverse=True)

    periods: list[ReportPeriod] = []
    for year, month in parsed:
        date_from, date_to, is_partial = _month_bounds(year, month, t)
        periods.append(ReportPeriod(_month_label(year, month), date_from, date_to, is_partial))
    return periods


def build_comparator_periods(
    months: list[str], *, today: date | None = None, max_years: int = 4
) -> list[ReportPeriod]:
    """The same calendar month across up to 4 different years (task-42 §3.1
    `kind=comparator`), newest first.
    """
    if not 2 <= len(months) <= max_years:
        raise InvalidPeriodError(f"comparator report needs 2-{max_years} months")

    t = today or date.today()
    parsed = sorted({_parse_month(m) for m in months}, reverse=True)
    month_numbers = {month for _year, month in parsed}
    if len(month_numbers) != 1:
        raise InvalidComparatorMonthsError(
            "All comparator months must share the same calendar month "
            "(e.g. 2026-07, 2025-07) — for arbitrary months, use kind=custom_months"
        )

    periods: list[ReportPeriod] = []
    for year, month in parsed:
        date_from, date_to, is_partial = _month_bounds(year, month, t)
        periods.append(ReportPeriod(_month_label(year, month), date_from, date_to, is_partial))
    return periods
