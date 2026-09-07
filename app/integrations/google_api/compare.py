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
}


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
    if a == 0:
        return None if b == 0 else Decimal("Infinity")
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
