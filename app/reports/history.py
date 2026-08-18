"""The monthly "Historical Data" block (task-45 §2.4/§1.5).

One row per COMPLETE calendar month — `Date · Clicks · Impressions · CTR ·
Conversions` — rendered under the main table and written as its own XLSX
sheet. It restores the history the operator used to keep by hand in Google
Sheets, which task-42 deliberately dropped for having no equivalent here.

Two queries per run, ~2s total:

- GSC: ONE day-dimensioned query over the whole window
  (`app.reports.api_source.fetch_gsc_daily`), grouped by month here. That
  single request doubles as the §1.6 discriminator: a month with no day rows
  is `None`, a month with day rows is a true sum, and a genuinely zero day
  stays `0`.
- GA4: ONE `yearMonth` x `firstUserDefaultChannelGroup` query for organic key
  events (`fetch_ga4_monthly_channel_conversions`).

`CTR` is derived, not fetched: `clicks / impressions`, verified live against
Reporting Ninja's own `ctr` field (EFFYS July 2026: 2 528 / 31 564 = 8.01% vs
8.009%). The XLSX writes it as a live formula so a row pasted into Google
Sheets keeps working, exactly as the hand-kept sheet did.

The current, still-open month is never included — the history is a record of
closed months, and a half-month row next to full ones would be a false
comparison (PLAN §12).
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date
from typing import Any

from app.reports.api_source import (
    GscDailyRow,
    fetch_ga4_monthly_channel_conversions,
    fetch_gsc_daily,
)
from app.reports.ninja_api import ReportingNinjaClient


@dataclass(frozen=True)
class HistoryMonth:
    month: str  # YYYY-MM
    label: str  # "July 2026"
    clicks: int | None = None
    impressions: int | None = None
    ctr: float | None = None
    organic_conversions: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "month": self.month,
            "label": self.label,
            "clicks": self.clicks,
            "impressions": self.impressions,
            "ctr": self.ctr,
            "organic_conversions": self.organic_conversions,
        }


@dataclass(frozen=True)
class ReportHistory:
    """Oldest month first — the order the hand-kept sheet used, and the order
    a reader scans a history in.
    """

    months: list[HistoryMonth]
    has_gsc: bool = True
    has_ga4: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "months": [m.to_dict() for m in self.months],
            "has_gsc": self.has_gsc,
            "has_ga4": self.has_ga4,
        }


def month_keys(*, count: int, today: date | None = None) -> list[str]:
    """The last `count` COMPLETE months as `YYYY-MM`, oldest first.

    Anchored to the month before `today`'s, so on 17 August 2026 the newest
    row is July 2026 — never a partial August.
    """
    if count < 1:
        return []
    reference = today or date.today()
    year, month = reference.year, reference.month - 1
    if month == 0:
        year, month = year - 1, 12

    keys: list[str] = []
    for _ in range(count):
        keys.append(f"{year:04d}-{month:02d}")
        month -= 1
        if month == 0:
            year, month = year - 1, 12
    return list(reversed(keys))


def window_bounds(keys: list[str]) -> tuple[date, date]:
    """`(first day of the oldest month, last day of the newest month)` — the
    single date range both provider queries use.
    """
    first_year, first_month = (int(part) for part in keys[0].split("-"))
    last_year, last_month = (int(part) for part in keys[-1].split("-"))
    return (
        date(first_year, first_month, 1),
        date(last_year, last_month, calendar.monthrange(last_year, last_month)[1]),
    )


def month_label(key: str) -> str:
    year, month = (int(part) for part in key.split("-"))
    return date(year, month, 1).strftime("%B %Y")


def compute_ctr(clicks: int | None, impressions: int | None) -> float | None:
    """`clicks / impressions`, or `None` when it is not a number: a missing
    side, or zero impressions (dividing by which is undefined, not 0.00% —
    PLAN §12, same rule as `table.safe_pct`).
    """
    if clicks is None or impressions is None or impressions == 0:
        return None
    return clicks / impressions


def group_daily_by_month(rows: list[GscDailyRow]) -> dict[str, tuple[int, int]]:
    """`{"2026-07": (clicks, impressions)}` from day rows. A month with no day
    row at all is simply absent — that absence is the "no data" signal the
    caller turns into `None`.
    """
    grouped: dict[str, tuple[int, int]] = {}
    for row in rows:
        day = row.date
        if len(day) < 7 or day[4] != "-":
            continue
        key = day[:7]
        clicks, impressions = grouped.get(key, (0, 0))
        grouped[key] = (clicks + (row.clicks or 0), impressions + (row.impressions or 0))
    return grouped


def build_history(
    *,
    keys: list[str],
    daily_rows: list[GscDailyRow],
    monthly_conversions: dict[str, int],
    has_gsc: bool = True,
    has_ga4: bool = True,
) -> ReportHistory:
    by_month = group_daily_by_month(daily_rows) if has_gsc else {}
    months: list[HistoryMonth] = []
    for key in keys:
        clicks, impressions = (None, None)
        if key in by_month:
            clicks, impressions = by_month[key]
        conversions = monthly_conversions.get(key) if has_ga4 else None
        months.append(
            HistoryMonth(
                month=key,
                label=month_label(key),
                clicks=clicks,
                impressions=impressions,
                ctr=compute_ctr(clicks, impressions),
                organic_conversions=conversions,
            )
        )
    return ReportHistory(months=months, has_gsc=has_gsc, has_ga4=has_ga4)


def fetch_history(
    client: ReportingNinjaClient,
    *,
    ga4_connection_key: str,
    ga4_account_id: str,
    ga4_fields: dict[str, str],
    gsc_ref: tuple[str, str] | None,
    gsc_fields: dict[str, str] | None,
    months: int = 12,
    today: date | None = None,
) -> ReportHistory:
    """Both queries plus the grouping. A client without a `gsc` integration
    still gets the block — `Clicks`/`Impressions`/`CTR` stay `None` and the UI
    says why, rather than the whole section disappearing (task-45 §2.4).
    """
    keys = month_keys(count=months, today=today)
    if not keys:
        return ReportHistory(months=[], has_gsc=gsc_ref is not None, has_ga4=True)
    date_from, date_to = window_bounds(keys)

    daily_rows: list[GscDailyRow] = []
    has_gsc = gsc_ref is not None and gsc_fields is not None and "date" in (gsc_fields or {})
    if has_gsc and gsc_ref is not None and gsc_fields is not None:
        gsc_connection_key, gsc_account_id = gsc_ref
        daily_rows = fetch_gsc_daily(
            client, gsc_connection_key, gsc_account_id, date_from, date_to, gsc_fields
        )

    has_ga4 = "month" in ga4_fields and "channel" in ga4_fields
    monthly_conversions: dict[str, int] = {}
    if has_ga4:
        monthly_conversions = fetch_ga4_monthly_channel_conversions(
            client, ga4_connection_key, ga4_account_id, date_from, date_to, ga4_fields
        )

    return build_history(
        keys=keys,
        daily_rows=daily_rows,
        monthly_conversions=monthly_conversions,
        has_gsc=has_gsc,
        has_ga4=has_ga4,
    )
