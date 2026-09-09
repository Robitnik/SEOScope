"""Pure transport-comparison report for `compare-transports` (task-83 §7).

No I/O, no DB. The CLI fetches the same window through both transports into
memory and hands the two `date -> {metric: value}` maps here.

Gate G2 (§7.3): integer metrics (`clicks`, `impressions`, all three key-event
counts) must match byte-for-byte — Reporting Ninja proxies the very same
Google API, so any difference means a different sample (`search_type`,
property type, timezone) and has to be understood, not rounded away.
Derived/estimated metrics (`ctr`, `position`, `sessions`, `users`,
`engagement_rate`) carry a small relative tolerance.
"""

from __future__ import annotations

import statistics
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Literal

DaySeries = Mapping[date, Mapping[str, Decimal | None]]


@dataclass(frozen=True)
class MetricTolerance:
    name: str
    kind: Literal["exact", "relative"]
    limit_pct: Decimal  # ignored when kind == "exact"


def _exact(name: str) -> MetricTolerance:
    return MetricTolerance(name, "exact", Decimal(0))


def _relative(name: str, limit_pct: str) -> MetricTolerance:
    return MetricTolerance(name, "relative", Decimal(limit_pct))


# §7.3 — keyed by dataset_key.
TOLERANCES: dict[str, dict[str, MetricTolerance]] = {
    "gsc_daily": {
        "clicks": _exact("clicks"),
        "impressions": _exact("impressions"),
        "ctr": _relative("ctr", "0.5"),
        "position": _relative("position", "0.5"),
    },
    "ga4_daily": {
        "sessions": _relative("sessions", "1"),
        "users": _relative("users", "1"),
        "conversions": _exact("conversions"),
        "engagement_rate": _relative("engagement_rate", "1"),
    },
    "ga4_daily_channels": {
        "conversions_organic": _exact("conversions_organic"),
        "conversions_direct": _exact("conversions_direct"),
    },
    # task-89 §7.1: the four remaining client-page surfaces, extending the
    # gate beyond task-83's original three `#/trends` datasets. Aggregate
    # KPIs are a single-row special case of `compare_dimension_rows` (one
    # synthetic key); the rest are keyed by dimension_value (page URL, query
    # text, channel name, …), never by date.
    "gsc_kpi": {
        "clicks": _exact("clicks"),
        "impressions": _exact("impressions"),
        "avg_position": _relative("avg_position", "0.5"),
    },
    "ga4_kpi": {
        "users": _relative("users", "1"),
        "organic_users": _relative("organic_users", "1"),
        "total_conversions": _exact("total_conversions"),
        "organic_conversions": _exact("organic_conversions"),
    },
    "gsc_pages": {
        "clicks": _exact("clicks"),
        "impressions": _exact("impressions"),
        "ctr": _relative("ctr", "0.5"),
        "position": _relative("position", "0.5"),
    },
    "gsc_queries": {
        "clicks": _exact("clicks"),
        "impressions": _exact("impressions"),
        "ctr": _relative("ctr", "0.5"),
        "position": _relative("position", "0.5"),
    },
    "ga4_channels": {
        "users": _relative("users", "1"),
        "sessions": _relative("sessions", "1"),
        "conversions": _exact("conversions"),
        "engagement_rate": _relative("engagement_rate", "1"),
    },
    "ga4_landing_pages": {
        "users": _relative("users", "1"),
        "sessions": _relative("sessions", "1"),
        "conversions": _exact("conversions"),
        "engagement_rate": _relative("engagement_rate", "1"),
        "views": _exact("views"),
    },
    "ga4_events": {
        "events": _exact("events"),
        "conversions": _exact("conversions"),
        "users": _relative("users", "1"),
    },
    "ga4_devices": {
        "users": _relative("users", "1"),
        "sessions": _relative("sessions", "1"),
        "conversions": _exact("conversions"),
    },
    "ga4_countries": {
        "users": _relative("users", "1"),
        "sessions": _relative("sessions", "1"),
        "conversions": _exact("conversions"),
    },
}

# task-89 §7.1 correction (found live on SHB, 2026-09-09): a dimension row's
# PRESENCE has no settling period — it is either in this period's answer or
# it is not. But its aggregate VALUE is a sum across every day in the window,
# and the tail days of that window are exactly as unsettled as they are for
# `ga4_daily`/`gsc_daily`. `ga4_landing_pages`/`ga4_events` failed `exact`
# tolerance by ±1 on SHB (a higher-volume client than HWELL, where this gate
# was first proven) purely from that lag — not a real transport mismatch.
# The caller (`_compare_dimension_dataset`) now shrinks the *fetch window*
# by `GSC_LAG_DAYS`/`GA4_LAG_DAYS` before calling here, so this module still
# does not need a `lag_days` filter of its own — `lag_days` below is display
# metadata only, showing what the caller already excluded.
DIMENSION_KEYED_DATASETS: frozenset[str] = frozenset({
    "gsc_kpi", "ga4_kpi", "gsc_pages", "gsc_queries",
    "ga4_channels", "ga4_landing_pages", "ga4_events", "ga4_devices", "ga4_countries",
})


@dataclass(frozen=True)
class DayDiff:
    day: date
    metric: str
    a: Decimal | None
    b: Decimal | None
    abs_diff: Decimal | None
    rel_pct: Decimal | None
    within_tolerance: bool
    note: str = ""


@dataclass
class MetricSummary:
    metric: str
    kind: str
    limit_pct: Decimal
    compared: int
    failures: int
    max_abs_diff: Decimal
    median_abs_diff: Decimal
    max_rel_pct: Decimal | None


@dataclass
class ComparisonReport:
    dataset: str
    label_a: str
    label_b: str
    lag_days: int
    days_compared: int
    days_only_a: list[date]
    days_only_b: list[date]
    failures: list[DayDiff] = field(default_factory=list)
    per_metric: list[MetricSummary] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.failures and not self.days_only_a and not self.days_only_b


def _rel_pct(a: Decimal, b: Decimal) -> Decimal | None:
    """Relative difference as a percentage of `a`.

    `a == b == 0` is a genuine PASS (0% difference), not "undefined" — found
    live while extending the gate to per-page/per-channel rows (task-89
    §7.1): a low-traffic page can legitimately have 0 clicks and 0
    impressions on BOTH sides at once, giving `ctr = 0` on both, and the
    caller's tolerance check treats `None` as "cannot verify, fail" — so
    returning `None` here for that case silently failed ~two thirds of
    HWELL's page rows on a metric that was, in fact, identical. This
    function is shared with `compare_series` (task-83's original date-keyed
    gate), so the fix applies there too; the edge case was simply never
    exercised there (a client's daily aggregate ctr is essentially always
    non-zero for an active property)."""
    if a == 0:
        return Decimal(0) if b == 0 else Decimal("Infinity")
    return (abs(a - b) / abs(a)) * Decimal(100)


def compare_series(
    dataset: str,
    series_a: DaySeries,
    series_b: DaySeries,
    *,
    label_a: str,
    label_b: str,
    lag_days: int,
    today: date,
) -> ComparisonReport:
    """Compare two day-keyed series for one dataset.

    Days inside the lag window (`today - lag_days .. today`) are excluded on
    both sides — there both sources legally disagree because the data is still
    settling (§7.4). A day present on only one side is reported but never
    diffed.
    """
    tolerances = TOLERANCES[dataset]
    cutoff = today - timedelta(days=lag_days)
    a_days = {day for day in series_a if day <= cutoff}
    b_days = {day for day in series_b if day <= cutoff}
    common = sorted(a_days & b_days)

    failures: list[DayDiff] = []
    abs_by_metric: dict[str, list[Decimal]] = {name: [] for name in tolerances}
    rel_by_metric: dict[str, list[Decimal]] = {name: [] for name in tolerances}
    fail_by_metric: dict[str, int] = {name: 0 for name in tolerances}
    compared_by_metric: dict[str, int] = {name: 0 for name in tolerances}

    for day in common:
        row_a = series_a[day]
        row_b = series_b[day]
        for name, tol in tolerances.items():
            a = row_a.get(name)
            b = row_b.get(name)
            if a is None and b is None:
                continue
            compared_by_metric[name] += 1
            if a is None or b is None:
                fail_by_metric[name] += 1
                failures.append(
                    DayDiff(
                        day=day, metric=name, a=a, b=b, abs_diff=None, rel_pct=None,
                        within_tolerance=False,
                        note=f"present only in {label_b if a is None else label_a}",
                    )
                )
                continue
            abs_diff = abs(a - b)
            rel = _rel_pct(a, b)
            abs_by_metric[name].append(abs_diff)
            if rel is not None and rel.is_finite():
                rel_by_metric[name].append(rel)
            if tol.kind == "exact":
                ok = abs_diff == 0
            else:
                ok = rel is not None and rel.is_finite() and rel <= tol.limit_pct
            if not ok:
                fail_by_metric[name] += 1
                failures.append(
                    DayDiff(
                        day=day, metric=name, a=a, b=b, abs_diff=abs_diff, rel_pct=rel,
                        within_tolerance=False,
                    )
                )

    per_metric = [
        MetricSummary(
            metric=name,
            kind=tol.kind,
            limit_pct=tol.limit_pct,
            compared=compared_by_metric[name],
            failures=fail_by_metric[name],
            max_abs_diff=max(abs_by_metric[name], default=Decimal(0)),
            median_abs_diff=(
                Decimal(str(statistics.median(abs_by_metric[name])))
                if abs_by_metric[name]
                else Decimal(0)
            ),
            max_rel_pct=max(rel_by_metric[name], default=None),
        )
        for name, tol in tolerances.items()
    ]

    return ComparisonReport(
        dataset=dataset,
        label_a=label_a,
        label_b=label_b,
        lag_days=lag_days,
        days_compared=len(common),
        days_only_a=sorted(a_days - b_days),
        days_only_b=sorted(b_days - a_days),
        failures=failures,
        per_metric=per_metric,
    )


# ── task-89 §7.1: dimension-keyed comparison (pages, queries, GA4 breakdowns,
# and the two KPI aggregates as a single-row special case) ─────────────────
RowSeries = Mapping[str, Mapping[str, Decimal | None]]  # dimension_value -> {metric: value}


@dataclass(frozen=True)
class RowDiff:
    key: str
    metric: str
    a: Decimal | None
    b: Decimal | None
    abs_diff: Decimal | None
    rel_pct: Decimal | None
    within_tolerance: bool
    note: str = ""


@dataclass
class DimensionComparisonReport:
    dataset: str
    label_a: str
    label_b: str
    keys_compared: int
    keys_only_a: list[str]
    keys_only_b: list[str]
    failures: list[RowDiff] = field(default_factory=list)
    per_metric: list[MetricSummary] = field(default_factory=list)
    lag_days: int = 0

    @property
    def passed(self) -> bool:
        return not self.failures and not self.keys_only_a and not self.keys_only_b


def compare_dimension_rows(
    dataset: str,
    rows_a: RowSeries,
    rows_b: RowSeries,
    *,
    label_a: str,
    label_b: str,
    lag_days: int = 0,
) -> DimensionComparisonReport:
    """Compare two dimension-keyed row sets — GSC pages/queries, GA4
    breakdown datasets, or a single-row KPI aggregate (task-89 §7.1).

    Same tolerance discipline as `compare_series` (exact for integer counts,
    a small relative tolerance for derived/estimated metrics), but keyed by
    `dimension_value` (page URL, query text, channel name, or a synthetic
    `"total"` key for the KPI aggregates) instead of `date`.

    `lag_days` is display-only here: a row's presence has no settling period,
    but its aggregate value sums every day in the fetch window, so the
    caller must already have shrunk that window by the lag before calling —
    see the module-level note above `DIMENSION_KEYED_DATASETS`.
    """
    tolerances = TOLERANCES[dataset]
    a_keys = set(rows_a)
    b_keys = set(rows_b)
    common = sorted(a_keys & b_keys)

    failures: list[RowDiff] = []
    abs_by_metric: dict[str, list[Decimal]] = {name: [] for name in tolerances}
    rel_by_metric: dict[str, list[Decimal]] = {name: [] for name in tolerances}
    fail_by_metric: dict[str, int] = {name: 0 for name in tolerances}
    compared_by_metric: dict[str, int] = {name: 0 for name in tolerances}

    for key in common:
        row_a = rows_a[key]
        row_b = rows_b[key]
        for name, tol in tolerances.items():
            a = row_a.get(name)
            b = row_b.get(name)
            if a is None and b is None:
                continue
            compared_by_metric[name] += 1
            if a is None or b is None:
                fail_by_metric[name] += 1
                failures.append(
                    RowDiff(
                        key=key, metric=name, a=a, b=b, abs_diff=None, rel_pct=None,
                        within_tolerance=False,
                        note=f"present only in {label_b if a is None else label_a}",
                    )
                )
                continue
            abs_diff = abs(a - b)
            rel = _rel_pct(a, b)
            abs_by_metric[name].append(abs_diff)
            if rel is not None and rel.is_finite():
                rel_by_metric[name].append(rel)
            if tol.kind == "exact":
                ok = abs_diff == 0
            else:
                ok = rel is not None and rel.is_finite() and rel <= tol.limit_pct
            if not ok:
                fail_by_metric[name] += 1
                failures.append(
                    RowDiff(
                        key=key, metric=name, a=a, b=b, abs_diff=abs_diff, rel_pct=rel,
                        within_tolerance=False,
                    )
                )

    per_metric = [
        MetricSummary(
            metric=name,
            kind=tol.kind,
            limit_pct=tol.limit_pct,
            compared=compared_by_metric[name],
            failures=fail_by_metric[name],
            max_abs_diff=max(abs_by_metric[name], default=Decimal(0)),
            median_abs_diff=(
                Decimal(str(statistics.median(abs_by_metric[name])))
                if abs_by_metric[name]
                else Decimal(0)
            ),
            max_rel_pct=max(rel_by_metric[name], default=None),
        )
        for name, tol in tolerances.items()
    ]

    return DimensionComparisonReport(
        dataset=dataset,
        label_a=label_a,
        label_b=label_b,
        lag_days=lag_days,
        keys_compared=len(common),
        keys_only_a=sorted(a_keys - b_keys),
        keys_only_b=sorted(b_keys - a_keys),
        failures=failures,
        per_metric=per_metric,
    )
