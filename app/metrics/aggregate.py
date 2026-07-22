"""Aggregate stored history into an arbitrary date range (PLAN §21.1).

Extended periods (`quarter`/`year`/`custom`) are answered **without any new
collection**: zero API units, zero browser sessions. The price is that the
answer is only as deep as the history we happened to store, and this module
is what makes that price visible instead of hidden — every result comes with
a `CoverageDTO` and a `quality` that degrades to `partial` the moment a
single day of the range is unbacked (§12: a gap is `partial`, never a zero).

Aggregation rules (PLAN §21.1), in the order they matter:

- additive range metrics (clicks, impressions, users, conversions) — sum of
  the stored sub-periods;
- **CTR = Σclicks / Σimpressions** — never the mean of stored CTRs;
- **avg position** — mean weighted by impressions, flagged `is_derived=True`
  as an approximation; with no weights it is reported `unavailable` rather
  than as a naive mean, which would be a plainly wrong number;
- snapshot metrics (Ahrefs Health, Semrush AS) are **not summed**: the last
  valid snapshot inside the range wins;
- a metric whose unit cannot be summed and has no aggregation rule is
  reported `unavailable` — better a gap than an invented total.

The module is pure: it performs no I/O. Callers hand it rows already read
from `metric_values` (joined with their snapshot's `observed_at`, which
`metric_values` itself does not carry) as `StoredMetricValue`, which keeps
`app.metrics` free of any dependency on `app.db` (task-03's zone).
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

from pydantic import BaseModel, ConfigDict

from app.metrics.catalog import CATALOG, DERIVED_RATIO_RULES
from app.schemas.adapter import DateRange, MetricValueResult
from app.schemas.api import CoverageDTO
from app.schemas.enums import MetricUnit, PeriodRole, Provider, Quality
from app.schemas.metrics import MetricSpec

# `derived_key -> weight_key`: metrics aggregated as a weighted mean instead
# of a sum. Position is meaningless without traffic behind it — a day with 3
# impressions must not pull the quarter's average as hard as a day with
# 30 000 (PLAN §21.1).
WEIGHTED_AVERAGE_RULES: dict[str, str] = {
    "gsc_avg_position": "gsc_impressions",
}

# Units that are never additive. A metric with one of these units and no
# entry in DERIVED_RATIO_RULES/WEIGHTED_AVERAGE_RULES has no defined way to
# span sub-periods, so it is reported `unavailable` instead of summed.
_NON_ADDITIVE_UNITS: frozenset[MetricUnit] = frozenset(
    {MetricUnit.percent, MetricUnit.ratio, MetricUnit.position, MetricUnit.score}
)

# Worst-wins ordering used when several inputs of different quality feed one
# aggregate. Ranked by how much the number can be trusted, not alphabetically.
_QUALITY_RANK: dict[Quality, int] = {
    Quality.complete: 0,
    Quality.stale: 1,
    Quality.truncated: 2,
    Quality.partial: 3,
    Quality.unavailable: 4,
}

_ONE_DAY = timedelta(days=1)

# One stored sub-period, as `(period_start, period_end)`.
_Period = tuple[date, date]


class StoredMetricValue(BaseModel):
    """One persisted `metric_values` row, as the aggregator consumes it.

    Same fields as `MetricValueDTO` (minus the ids) plus `observed_at`,
    which lives on the owning `provider_snapshots` row and is what orders
    snapshot metrics and re-collections of the same period.
    """

    model_config = ConfigDict(extra="forbid")

    client_id: str
    provider: Provider
    metric_key: str
    period_role: PeriodRole
    period_start: date | None = None  # None for snapshot metrics
    period_end: date | None = None
    value_numeric: Decimal | None = None  # None means "no data", never 0
    unit: MetricUnit
    quality: Quality
    is_derived: bool = False
    observed_at: datetime


class _Aggregate(BaseModel):
    """Intermediate: one aggregated number plus the quality it earned."""

    model_config = ConfigDict(extra="forbid")

    value: Decimal | None = None
    quality: Quality = Quality.unavailable
    is_derived: bool = False


def _quantize(value: Decimal, precision: int) -> Decimal:
    return value.quantize(Decimal(1).scaleb(-precision), rounding=ROUND_HALF_UP)


def _worst(qualities: Iterable[Quality]) -> Quality:
    ranked = sorted(qualities, key=lambda q: _QUALITY_RANK[q])
    return ranked[-1] if ranked else Quality.unavailable


def _prefer(candidate: StoredMetricValue, incumbent: StoredMetricValue) -> bool:
    """Which of two rows for the same `(metric, period)` to keep.

    The newest observation wins — a re-collection supersedes what it
    corrected. Ties (same `observed_at`) break towards the better quality,
    then towards a directly collected value over a derived one.
    """
    return (
        candidate.observed_at,
        -_QUALITY_RANK[candidate.quality],
        not candidate.is_derived,
    ) > (
        incumbent.observed_at,
        -_QUALITY_RANK[incumbent.quality],
        not incumbent.is_derived,
    )


def _index_range_rows(
    rows: Iterable[StoredMetricValue], date_range: DateRange
) -> dict[str, dict[_Period, StoredMetricValue]]:
    """`metric_key -> period -> row` for rows fully inside `date_range`.

    Rows that only partially overlap the range are dropped, not clipped: we
    cannot know how a stored week's clicks split across the quarter
    boundary, and pro-rating them would fabricate data. Their in-range days
    stay uncovered and surface as a gap.
    """
    indexed: dict[str, dict[_Period, StoredMetricValue]] = {}
    for row in rows:
        if row.period_start is None or row.period_end is None:
            continue
        if row.period_start < date_range.start or row.period_end > date_range.end:
            continue
        if row.period_end < row.period_start:
            continue
        period: _Period = (row.period_start, row.period_end)
        by_period = indexed.setdefault(row.metric_key, {})
        incumbent = by_period.get(period)
        if incumbent is None or _prefer(row, incumbent):
            by_period[period] = row
    return indexed


def _select_periods(indexed: dict[str, dict[_Period, StoredMetricValue]]) -> list[_Period]:
    """Pick one non-overlapping set of stored sub-periods to aggregate over.

    Stored history overlaps itself: the same week is written as `current` by
    one job and as `previous` by the next, and a month may sit on top of the
    weeks inside it. Summing all of them would double-count, so we pick a
    disjoint cover — earliest start first, longest range first on a tie —
    and use the *same* set for every metric, which is what keeps derived
    values consistent (Σclicks and Σimpressions span identical days).
    """
    candidates = {
        period
        for by_period in indexed.values()
        for period, row in by_period.items()
        if row.value_numeric is not None
    }
    selected: list[_Period] = []
    last_end: date | None = None
    for start, end in sorted(candidates, key=lambda p: (p[0], -(p[1] - p[0]).days)):
        if last_end is not None and start <= last_end:
            continue
        selected.append((start, end))
        last_end = end
    return selected


def _coverage(selected: list[_Period], date_range: DateRange) -> CoverageDTO:
    days_total = (date_range.end - date_range.start).days + 1
    days_covered = sum((end - start).days + 1 for start, end in selected)

    gaps: list[DateRange] = []
    cursor = date_range.start
    for start, end in selected:
        if start > cursor:
            gaps.append(DateRange(start=cursor, end=start - _ONE_DAY))
        cursor = end + _ONE_DAY
    if cursor <= date_range.end:
        gaps.append(DateRange(start=cursor, end=date_range.end))

    if not selected:
        quality = Quality.unavailable
    elif gaps:
        quality = Quality.partial
    else:
        quality = Quality.complete
    return CoverageDTO(days_total=days_total, days_covered=days_covered, gaps=gaps, quality=quality)


def _sum_metric(
    metric_key: str,
    indexed: dict[str, dict[_Period, StoredMetricValue]],
    selected: list[_Period],
) -> _Aggregate:
    by_period = indexed.get(metric_key, {})
    present = [
        row
        for period in selected
        if (row := by_period.get(period)) is not None and row.value_numeric is not None
    ]
    if not present:
        return _Aggregate()
    total = sum(
        (row.value_numeric for row in present if row.value_numeric is not None), start=Decimal(0)
    )
    qualities = [row.quality for row in present]
    if len(present) < len(selected):
        qualities.append(Quality.partial)  # some sub-periods contributed nothing
    return _Aggregate(value=total, quality=_worst(qualities))


def _weighted_average(
    metric_key: str,
    weight_key: str,
    indexed: dict[str, dict[_Period, StoredMetricValue]],
    selected: list[_Period],
) -> _Aggregate:
    values = indexed.get(metric_key, {})
    weights = indexed.get(weight_key, {})

    contributing: list[tuple[Decimal, Decimal, Quality]] = []
    value_rows: list[StoredMetricValue] = []
    for period in selected:
        value_row = values.get(period)
        if value_row is None or value_row.value_numeric is None:
            continue
        value_rows.append(value_row)
        weight_row = weights.get(period)
        if weight_row is None or weight_row.value_numeric is None or weight_row.value_numeric <= 0:
            continue
        contributing.append(
            (
                value_row.value_numeric,
                weight_row.value_numeric,
                _worst([value_row.quality, weight_row.quality]),
            )
        )

    if not value_rows:
        return _Aggregate()
    if len(value_rows) == 1 and not contributing:
        # A single sub-period needs no weighting — the stored value already
        # describes exactly this range.
        only = value_rows[0]
        assert only.value_numeric is not None
        return _Aggregate(
            value=only.value_numeric, quality=only.quality, is_derived=only.is_derived
        )
    if not contributing:
        # Several sub-periods but nothing to weight them by: a plain mean
        # here would be exactly the wrong number PLAN §21.1 warns about.
        return _Aggregate()

    weight_total = sum((weight for _, weight, _ in contributing), start=Decimal(0))
    weighted = sum((value * weight for value, weight, _ in contributing), start=Decimal(0))
    qualities = [quality for _, _, quality in contributing]
    if len(contributing) < len(selected):
        qualities.append(Quality.partial)
    return _Aggregate(value=weighted / weight_total, quality=_worst(qualities), is_derived=True)


def _derived_ratio(
    spec: MetricSpec,
    indexed: dict[str, dict[_Period, StoredMetricValue]],
    selected: list[_Period],
) -> _Aggregate:
    numerator_key, denominator_key = DERIVED_RATIO_RULES[spec.key]
    numerator = _sum_metric(numerator_key, indexed, selected)
    denominator = _sum_metric(denominator_key, indexed, selected)
    if numerator.value is not None and denominator.value is not None and denominator.value != 0:
        ratio = (numerator.value / denominator.value) * Decimal(100)
        return _Aggregate(
            value=ratio,
            quality=_worst([numerator.quality, denominator.quality]),
            is_derived=True,
        )

    # No components — fall back to a directly stored ratio, but only when a
    # single sub-period covers the range, where no aggregation is involved.
    stored = indexed.get(spec.key, {})
    if len(selected) == 1:
        row = stored.get(selected[0])
        if row is not None and row.value_numeric is not None:
            return _Aggregate(value=row.value_numeric, quality=row.quality, is_derived=True)
    return _Aggregate()


def _aggregate_snapshot(
    spec: MetricSpec, snapshot_rows: list[StoredMetricValue], date_range: DateRange
) -> MetricValueResult:
    """Last valid snapshot inside the range — snapshots are never summed."""
    candidates = [
        row
        for row in snapshot_rows
        if row.metric_key == spec.key
        and row.value_numeric is not None
        and row.quality != Quality.unavailable
        and date_range.start <= row.observed_at.date() <= date_range.end
    ]
    if not candidates:
        return MetricValueResult(
            metric_key=spec.key,
            period_role=PeriodRole.snapshot,
            value_numeric=None,
            unit=spec.unit,
            quality=Quality.unavailable,
        )
    latest = max(candidates, key=lambda row: row.observed_at)
    assert latest.value_numeric is not None
    return MetricValueResult(
        metric_key=spec.key,
        period_role=PeriodRole.snapshot,
        value_numeric=_quantize(latest.value_numeric, spec.precision),
        unit=spec.unit,
        quality=latest.quality,
        is_derived=latest.is_derived,
    )


def _aggregate_range(
    spec: MetricSpec,
    indexed: dict[str, dict[_Period, StoredMetricValue]],
    selected: list[_Period],
    date_range: DateRange,
    period_role: PeriodRole,
    coverage_quality: Quality,
) -> MetricValueResult:
    if spec.key in DERIVED_RATIO_RULES:
        aggregate = _derived_ratio(spec, indexed, selected)
    elif spec.key in WEIGHTED_AVERAGE_RULES:
        aggregate = _weighted_average(spec.key, WEIGHTED_AVERAGE_RULES[spec.key], indexed, selected)
    elif spec.unit in _NON_ADDITIVE_UNITS:
        aggregate = _Aggregate()  # no rule to span sub-periods — refuse to invent one
    else:
        aggregate = _sum_metric(spec.key, indexed, selected)

    if aggregate.value is None:
        quality = Quality.unavailable
        value: Decimal | None = None
    else:
        quality = _worst([aggregate.quality, coverage_quality])
        value = _quantize(aggregate.value, spec.precision)

    return MetricValueResult(
        metric_key=spec.key,
        period_role=period_role,
        period_start=date_range.start,
        period_end=date_range.end,
        value_numeric=value,
        unit=spec.unit,
        quality=quality,
        is_derived=aggregate.is_derived,
    )


def aggregate_metrics(
    values: Iterable[StoredMetricValue],
    *,
    client_id: str,
    provider: Provider,
    date_range: DateRange,
    catalog: dict[str, MetricSpec] = CATALOG,
    period_role: PeriodRole = PeriodRole.current,
) -> tuple[list[MetricValueResult], CoverageDTO]:
    """Aggregate one `(client, provider)` cell's stored history into `date_range`.

    `values` may be a wider result set (several clients/providers); it is
    filtered here, so a caller can read once for a whole dashboard and
    aggregate cell by cell. Returns one `MetricValueResult` per catalog
    metric of `provider` — including `unavailable` ones, because "we have no
    data for this" is an answer the UI must render and is never a zero — plus
    the coverage of the range.

    Call it once per side of a comparison: `period_role=current` for
    `Periods.current`, `previous` for `Periods.previous`. Snapshot metrics
    ignore that argument and are always tagged `PeriodRole.snapshot`, which
    is what `quality.ensure_comparable_periods` requires of them.
    """
    if period_role == PeriodRole.snapshot:
        raise ValueError(
            "period_role=snapshot is assigned per metric by the aggregator; "
            "pass current or previous for the range side being aggregated"
        )
    if date_range.end < date_range.start:
        raise ValueError(f"date_range end {date_range.end} precedes start {date_range.start}")

    rows = [v for v in values if v.client_id == client_id and v.provider == provider]
    snapshot_rows = [r for r in rows if r.period_start is None and r.period_end is None]

    indexed = _index_range_rows(rows, date_range)
    selected = _select_periods(indexed)
    coverage = _coverage(selected, date_range)

    specs = [spec for spec in catalog.values() if spec.provider == provider]
    results: list[MetricValueResult] = []
    for spec in specs:
        if spec.comparison_kind == "snapshot":
            results.append(_aggregate_snapshot(spec, snapshot_rows, date_range))
        else:
            results.append(
                _aggregate_range(spec, indexed, selected, date_range, period_role, coverage.quality)
            )

    if not any(spec.comparison_kind == "range" for spec in specs):
        # Snapshot-only provider (Ahrefs, Semrush): day coverage is not a
        # meaningful question — a snapshot describes a moment, not a span.
        # Coverage then means "did the range contain a usable snapshot".
        coverage = _snapshot_coverage(results, date_range)
    return results, coverage


def _snapshot_coverage(results: list[MetricValueResult], date_range: DateRange) -> CoverageDTO:
    days_total = (date_range.end - date_range.start).days + 1
    has_snapshot = any(row.value_numeric is not None for row in results)
    if has_snapshot:
        return CoverageDTO(
            days_total=days_total,
            days_covered=days_total,
            gaps=[],
            quality=Quality.complete,
        )
    return CoverageDTO(
        days_total=days_total,
        days_covered=0,
        gaps=[DateRange(start=date_range.start, end=date_range.end)],
        quality=Quality.unavailable,
    )
