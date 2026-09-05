"""Shared period-series logic for GSC/GA4 page-scoped data (task-61
§0.3/§0.4). One implementation so `sources/gsc.py` and `sources/ga4.py`
can't drift on the two invariants measured against the real portfolio:

1. **Last snapshot wins per period.** The same `(period_start, period_end)`
   can be stored by up to 12 competing snapshots (GSC matures a week's
   numbers after the fact — one real period went 396 -> 470 clicks across
   two re-collections). Always keep the row from the latest `created_at`.

2. **Never average across period lengths.** The same table holds 7/14/26/
   30/90/365-day periods interleaved. A caller asks for one granularity and
   gets only periods whose length matches it; the rest are counted, never
   silently folded in (that would draw a fake ×5 spike where none exists —
   task-61 plan §0.4).

Honest absence, not a fabricated zero (PLAN §12): when this URL has no row
in an otherwise-`complete` period, the source provider returned every
nonzero row for that period and ours wasn't one of them — a real `0`. When
the period was `truncated` (cut to top-N), the URL's absence is genuinely
unknown — it may be below the cutoff, not necessarily zero — so the point
is `None` with `absence_reason="truncated"`. A period this URL's provider
never collected AT ALL is `None` with `absence_reason="not_collected"`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from app.schemas.enums import Quality
from app.schemas.url_inspector import Granularity, UrlSeriesDTO, UrlSeriesPointDTO

PeriodKey = tuple[date, date]


@dataclass(frozen=True)
class PeriodRow:
    """One stored row already known to belong to a single URL (or, for
    `PeriodExistence` below, a period-representative row) — the smallest
    shape both `gsc.py` and `ga4.py` can build from their own ORM rows
    without this module needing to know either table."""

    period_start: date
    period_end: date
    created_at: datetime
    quality: Quality
    value: Decimal | None


def _period_days(period_start: date, period_end: date) -> int:
    return (period_end - period_start).days + 1


def _bucket_of(days: int) -> Granularity | None:
    if days == 7:
        return "week"
    if 28 <= days <= 31:
        return "month"
    if 89 <= days <= 92:
        return "quarter"
    return None


def _latest_by_period(rows: list[PeriodRow]) -> dict[PeriodKey, PeriodRow]:
    latest: dict[PeriodKey, PeriodRow] = {}
    for row in rows:
        key = (row.period_start, row.period_end)
        current = latest.get(key)
        if current is None or row.created_at >= current.created_at:
            latest[key] = row
    return latest


def build_series(
    *,
    metric: str,
    unit: str,
    url_rows: list[PeriodRow],
    period_existence: list[PeriodRow],
    granularity: Granularity,
) -> UrlSeriesDTO:
    """`url_rows` — rows that matched one of this URL's lookup variants
    (`value` is this URL's own metric value). `period_existence` — one
    representative row per period for the WHOLE client+dataset (any URL),
    used only for its `quality`/existence when `url_rows` has nothing for
    that period; its own `value` is ignored.
    """
    url_by_period = _latest_by_period(url_rows)
    existence_by_period = _latest_by_period(period_existence)

    all_periods = set(url_by_period) | set(existence_by_period)
    points: list[UrlSeriesPointDTO] = []
    other_length = 0

    for period_start, period_end in sorted(all_periods):
        days = _period_days(period_start, period_end)
        bucket = _bucket_of(days)
        if bucket != granularity:
            if bucket is not None:
                other_length += 1
            continue

        matched = url_by_period.get((period_start, period_end))
        if matched is not None:
            points.append(
                UrlSeriesPointDTO(
                    period_start=period_start,
                    period_end=period_end,
                    period_days=days,
                    value=matched.value,
                    absence_reason=None,
                    quality=matched.quality,
                    observed_at=matched.created_at,
                )
            )
            continue

        existed = existence_by_period.get((period_start, period_end))
        if existed is None:
            # Unreachable in practice: `all_periods` is the UNION of both
            # dicts' keys, so a key absent from `url_by_period` here is
            # guaranteed present in `existence_by_period`. Kept as a guard
            # rather than an `assert` so a future caller passing
            # inconsistent inputs degrades to "period silently dropped",
            # never a raised exception on a read endpoint.
            continue
        if existed.quality == Quality.truncated:
            points.append(
                UrlSeriesPointDTO(
                    period_start=period_start,
                    period_end=period_end,
                    period_days=days,
                    value=None,
                    absence_reason="truncated",
                    quality=existed.quality,
                    observed_at=existed.created_at,
                )
            )
        else:
            # Provider returned every nonzero row for a `complete` period and
            # this URL wasn't one of them — an honest 0, not a guess.
            points.append(
                UrlSeriesPointDTO(
                    period_start=period_start,
                    period_end=period_end,
                    period_days=days,
                    value=Decimal(0),
                    absence_reason=None,
                    quality=existed.quality,
                    observed_at=existed.created_at,
                )
            )

    return UrlSeriesDTO(
        metric=metric,
        unit=unit,  # type: ignore[arg-type]
        granularity=granularity,
        points=points,
        other_length_periods=other_length,
    )
