"""Portfolio Trends engine (task-28): one line per client across the whole
portfolio for a single daily metric (GSC clicks/impressions, GA4
sessions/users), bucketed into day/week/month buckets.

Pure, side-effect-free — same precedent as `app.metrics.alerts`: takes a
plain `date -> Decimal` daily series already resolved by
`AnalyticsDatasetRepository.daily_series`, never touches the ORM. A day
absent from the input mapping means "not collected", never `0` (PLAN §12);
a real collected `0` stays `0` because it is a present key.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict

from app.metrics.catalog import CATALOG
from app.schemas.enums import BetterDirection, MetricUnit, Provider, Quality

Bucket = Literal["day", "week", "month"]

# GSC reports the last ~3 days incomplete; GA4 (via Reporting Ninja) the last
# ~2. A bucket touching that window is `provisional`, never compared as if
# finished (plan §1.5/§3.3).
GSC_LAG_DAYS = 3
GA4_LAG_DAYS = 2

MAX_WINDOW_DAYS = 400


@dataclass(frozen=True)
class SeriesSpec:
    """One selectable portfolio series.

    `label`/`unit`/`better_direction` come from `app.metrics.CATALOG` where a
    matching key exists there, so a metric's name never drifts between the
    client card and this page (CLAUDE.md: provider/metric named identically
    everywhere). `rn_sessions` has no catalog entry — no adapter writes a
    `MetricValue` for it, only the `ga4_daily` dataset row — so it is
    described here instead (plan §4.2 explains why it does not get added to
    `CATALOG`: that would put an empty "Sessions" row on every client card).
    """

    key: str
    label: str
    provider: Provider
    dataset_key: str
    field: str
    unit: MetricUnit
    better_direction: BetterDirection
    lag_days: int
    # task-44: whether summing daily values into a week/month bucket is
    # mathematically correct. Event-shaped metrics (clicks, sessions, key
    # events/conversions) are additive — the sum of daily counts equals the
    # count for the period. `users` is NOT: it counts unique visitors, and
    # summing daily uniques double-counts anyone who returns on more than one
    # day within the bucket (measured +17-23% on a real client, see this
    # task's plan §1.4). The page must show a note rather than pretend the
    # bucketed number is a real unique count.
    additive: bool


def _from_catalog(
    key: str, *, dataset_key: str, field: str, lag_days: int, additive: bool
) -> SeriesSpec:
    spec = CATALOG[key]
    return SeriesSpec(
        key=key,
        label=spec.label,
        provider=spec.provider,
        dataset_key=dataset_key,
        field=field,
        unit=spec.unit,
        better_direction=spec.better_direction,
        lag_days=lag_days,
        additive=additive,
    )


PORTFOLIO_SERIES: dict[str, SeriesSpec] = {
    spec.key: spec
    for spec in (
        _from_catalog(
            "gsc_clicks",
            dataset_key="gsc_daily",
            field="clicks",
            lag_days=GSC_LAG_DAYS,
            additive=True,
        ),
        _from_catalog(
            "gsc_impressions",
            dataset_key="gsc_daily",
            field="impressions",
            lag_days=GSC_LAG_DAYS,
            additive=True,
        ),
        SeriesSpec(
            key="rn_sessions",
            label="Sessions",
            provider=Provider.reporting_ninja,
            dataset_key="ga4_daily",
            field="sessions",
            unit=MetricUnit.count,
            better_direction=BetterDirection.up,
            lag_days=GA4_LAG_DAYS,
            additive=True,
        ),
        _from_catalog(
            "rn_users",
            dataset_key="ga4_daily",
            field="users",
            lag_days=GA4_LAG_DAYS,
            additive=False,
        ),
        # task-44: GA4 key events already collected in full by `ga4_daily`
        # since day one — no new dataset, no backfill (plan §1.1).
        _from_catalog(
            "rn_total_conversions",
            dataset_key="ga4_daily",
            field="conversions",
            lag_days=GA4_LAG_DAYS,
            additive=True,
        ),
        # task-44: `firstUserDefaultChannelGroup` breakdown, same attribution
        # as the client report (task-42) — see plan §1.3 for why NOT
        # `ga4_channels`'s `sessionDefaultChannelGroup`.
        _from_catalog(
            "rn_organic_conversions",
            dataset_key="ga4_daily_channels",
            field="conversions_organic",
            lag_days=GA4_LAG_DAYS,
            additive=True,
        ),
        # No CATALOG entry for `rn_direct_conversions` — same reason as
        # `rn_sessions` above: no adapter writes a `MetricValue` for it, only
        # this dataset row, so a catalog entry would put an empty "Direct
        # Conversions" row on every client card (plan §3.1).
        SeriesSpec(
            key="rn_direct_conversions",
            label="Direct Conversions",
            provider=Provider.reporting_ninja,
            dataset_key="ga4_daily_channels",
            field="conversions_direct",
            unit=MetricUnit.count,
            better_direction=BetterDirection.up,
            lag_days=GA4_LAG_DAYS,
            additive=True,
        ),
    )
}


class BucketRange(BaseModel):
    """One time bucket in the requested window.

    `days_expected` is the *actual* number of days this bucket covers inside
    the requested window — a week/month clipped at the window edge is not
    penalized as partial just for being clipped (plan §3.4).
    """

    model_config = ConfigDict(extra="forbid")

    start: date
    end: date
    days_expected: int
    provisional: bool


class SeriesPoint(BaseModel):
    """One bucket's aggregate for one client.

    `value=None` iff `days_with_data == 0` — absence, never a fabricated
    zero (PLAN §12). A real collected `0` is preserved as `Decimal(0)`.
    """

    model_config = ConfigDict(extra="forbid")

    value: Decimal | None
    days_with_data: int
    quality: Quality


class ClientAggregate(BaseModel):
    """One client's full bucketed series plus overall coverage."""

    model_config = ConfigDict(extra="forbid")

    points: list[SeriesPoint]
    first_day: date | None
    last_day: date | None
    days_with_data: int
    days_expected: int


def _week_start(day: date) -> date:
    return day - timedelta(days=day.weekday())


def _month_start(day: date) -> date:
    return day.replace(day=1)


def _month_end(month_start: date) -> date:
    next_month_start = (
        date(month_start.year + 1, 1, 1)
        if month_start.month == 12
        else date(month_start.year, month_start.month + 1, 1)
    )
    return next_month_start - timedelta(days=1)


def build_buckets(
    start: date,
    end: date,
    bucket: Bucket,
    *,
    today: date,
    lag_days: int,
) -> list[BucketRange]:
    """Split `[start, end]` (inclusive) into buckets of the requested kind.

    `week` buckets are ISO weeks (Monday start); `month` buckets are calendar
    months. Both are clipped to the requested window at the edges, and
    `days_expected` reflects the clipped span, not the full 7/28-31.
    """
    if end < start:
        raise ValueError(f"end {end} precedes start {start}")

    spans: list[tuple[date, date]] = []
    if bucket == "day":
        cursor = start
        while cursor <= end:
            spans.append((cursor, cursor))
            cursor += timedelta(days=1)
    elif bucket == "week":
        cursor = start
        while cursor <= end:
            week_start = _week_start(cursor)
            week_end = week_start + timedelta(days=6)
            clipped_start = max(week_start, start)
            clipped_end = min(week_end, end)
            spans.append((clipped_start, clipped_end))
            cursor = clipped_end + timedelta(days=1)
    elif bucket == "month":
        cursor = start
        while cursor <= end:
            month_start = _month_start(cursor)
            month_end = _month_end(month_start)
            clipped_start = max(month_start, start)
            clipped_end = min(month_end, end)
            spans.append((clipped_start, clipped_end))
            cursor = clipped_end + timedelta(days=1)
    else:  # pragma: no cover - Literal exhausted by callers/FastAPI validation
        raise ValueError(f"unknown bucket kind: {bucket}")

    lag_cutoff = today - timedelta(days=lag_days)
    return [
        BucketRange(
            start=span_start,
            end=span_end,
            days_expected=(span_end - span_start).days + 1,
            provisional=span_end >= lag_cutoff,
        )
        for span_start, span_end in spans
    ]


def aggregate_client_series(
    daily: Mapping[date, Decimal],
    buckets: Sequence[BucketRange],
) -> ClientAggregate:
    """Sum `daily` values into each bucket.

    A day missing from `daily` does not count toward `days_with_data` and
    does not contribute to the sum — the only way a point's `value` becomes
    `None` is `days_with_data == 0` for that bucket, never an assumed zero.
    """
    points: list[SeriesPoint] = []
    total_with_data = 0
    total_expected = 0
    for span in buckets:
        span_days = (span.end - span.start).days + 1
        total_expected += span.days_expected
        values = [
            daily[span.start + timedelta(days=offset)]
            for offset in range(span_days)
            if (span.start + timedelta(days=offset)) in daily
        ]
        days_with_data = len(values)
        total_with_data += days_with_data
        if days_with_data == 0:
            points.append(
                SeriesPoint(value=None, days_with_data=0, quality=Quality.unavailable)
            )
            continue
        value = sum(values, Decimal(0))
        quality = (
            Quality.complete if days_with_data == span.days_expected else Quality.partial
        )
        points.append(SeriesPoint(value=value, days_with_data=days_with_data, quality=quality))

    days_present = sorted(daily.keys())
    return ClientAggregate(
        points=points,
        first_day=days_present[0] if days_present else None,
        last_day=days_present[-1] if days_present else None,
        days_with_data=total_with_data,
        days_expected=total_expected,
    )
