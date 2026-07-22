"""Alerts engine (PLAN §21.5, task-17): pages/queries whose clicks are
declining or rising sharply between two adjacent periods.

Pure, side-effect-free — takes rows already resolved for both periods
(`DimensionRepository.for_two_periods`, task-16) mapped into `DimensionRow`,
and classifies them. No DB access, so `app.metrics` stays free of any
dependency on `app.db` (task-03's zone) — same precedent as
`app.metrics.aggregate.StoredMetricValue`.

Detection rules (PLAN §21.5):

- relative change >= threshold (default 20%) **and** base-period volume
  >= minimum (default >=50 impressions or >=10 clicks) — a `1 -> 2` click
  move must never fire;
- sorted by **absolute impact** (clicks lost/gained), never by percentage —
  `5000 -> 4000` outranks `20 -> 10` despite the smaller percentage;
- a `dimension_value` absent from the previous period is `new`, never
  "infinite growth";
- the top-N cut's aggregated remainder row (`rank=None`, task-16's
  `DIMENSION_OTHER_VALUE`) is excluded from classification — it names a
  different set of pages each period, so comparing it across periods is not
  meaningful;
- if either period carries a `partial`/`truncated` row, or either period has
  no stored data at all, no alerts are built — a false alert is worse than
  none (PLAN §21.5 risk).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from pydantic import BaseModel, ConfigDict

from app.schemas.enums import Quality

DEFAULT_MIN_RELATIVE_CHANGE_PCT = Decimal("20")
DEFAULT_MIN_BASE_IMPRESSIONS = 50
DEFAULT_MIN_BASE_CLICKS = 10
DEFAULT_TOP_N_PER_GROUP = 20


@dataclass(frozen=True)
class AlertThresholds:
    """Noise-control knobs, sourced from `Settings` — never hardcoded at a
    call site (task-17's Definition of Done)."""

    min_relative_change_pct: Decimal = DEFAULT_MIN_RELATIVE_CHANGE_PCT
    min_base_impressions: int = DEFAULT_MIN_BASE_IMPRESSIONS
    min_base_clicks: int = DEFAULT_MIN_BASE_CLICKS
    top_n_per_group: int = DEFAULT_TOP_N_PER_GROUP


_DEFAULT_THRESHOLDS = AlertThresholds()


class DimensionRow(BaseModel):
    """One page/query row for one period, as `detect_alerts` consumes it —
    decoupled from `GscDimensionMetric` (see module docstring). `rank=None`
    marks the top-N cut's aggregated remainder row. `clicks`/`impressions`
    are `None` only when genuinely unobserved for this row — never coerced
    to `0` (PLAN §12: `None` != `0`)."""

    model_config = ConfigDict(extra="forbid")

    dimension_value: str
    clicks: int | None
    impressions: int | None
    quality: Quality
    rank: int | None


class AlertEntry(BaseModel):
    """One flagged page/query with its absolute and relative click change.

    `delta_pct` is `None` whenever the base period had `0` clicks — the
    percentage is undefined there, never reported as `inf` (PLAN §12)."""

    model_config = ConfigDict(extra="forbid")

    dimension_value: str
    clicks_current: int | None
    clicks_previous: int | None
    delta_abs: int
    delta_pct: Decimal | None


class AlertsResult(BaseModel):
    """Result of `detect_alerts`, grouped and sorted by absolute impact.

    `skipped_reason` is set (with the three lists left empty) whenever
    either period carries a `partial`/`truncated` row or has no stored data
    at all — a false alert is worse than no alert (PLAN §21.5)."""

    model_config = ConfigDict(extra="forbid")

    declining: list[AlertEntry] = []
    rising: list[AlertEntry] = []
    new: list[AlertEntry] = []
    skipped_reason: str | None = None


def _quality_gate_reason(
    pages_current: Sequence[DimensionRow], pages_previous: Sequence[DimensionRow]
) -> str | None:
    if not pages_current or not pages_previous:
        return "missing_period_data"
    for label, rows in (("current", pages_current), ("previous", pages_previous)):
        for row in rows:
            if row.quality in (Quality.partial, Quality.truncated):
                return f"{label}_period_{row.quality.value}"
    return None


def _passes_base_volume(row: DimensionRow, thresholds: AlertThresholds) -> bool:
    """Whether `row` (the base/previous-period side) clears the noise floor.

    A `None` count never *satisfies* a minimum here — treating it as `0`
    only ever makes the gate stricter, and the substituted value is never
    surfaced in output (PLAN §12's `None` != `0` governs reported values,
    not this internal filter)."""

    impressions = row.impressions if row.impressions is not None else 0
    clicks = row.clicks if row.clicks is not None else 0
    return impressions >= thresholds.min_base_impressions or clicks >= thresholds.min_base_clicks


def _classify(
    current: DimensionRow, previous: DimensionRow, thresholds: AlertThresholds
) -> AlertEntry | None:
    if current.clicks is None or previous.clicks is None:
        return None
    if not _passes_base_volume(previous, thresholds):
        return None

    delta_abs = current.clicks - previous.clicks
    if delta_abs == 0:
        return None

    if previous.clicks == 0:
        # Base is zero clicks: percentage change is undefined, not infinite.
        # The base-volume gate above already required qualifying impressions
        # or clicks, so this is not noise despite the missing percentage.
        delta_pct: Decimal | None = None
    else:
        delta_pct = ((Decimal(delta_abs) / Decimal(previous.clicks)) * 100).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        if abs(delta_pct) < thresholds.min_relative_change_pct:
            return None

    return AlertEntry(
        dimension_value=current.dimension_value,
        clicks_current=current.clicks,
        clicks_previous=previous.clicks,
        delta_abs=delta_abs,
        delta_pct=delta_pct,
    )


def _new_entry(current: DimensionRow, thresholds: AlertThresholds) -> AlertEntry | None:
    if current.clicks is None:
        return None
    if not _passes_base_volume(current, thresholds):
        return None
    return AlertEntry(
        dimension_value=current.dimension_value,
        clicks_current=current.clicks,
        clicks_previous=None,
        delta_abs=current.clicks,
        delta_pct=None,
    )


def detect_alerts(
    pages_current: Sequence[DimensionRow],
    pages_previous: Sequence[DimensionRow],
    thresholds: AlertThresholds = _DEFAULT_THRESHOLDS,
) -> AlertsResult:
    """Classify `pages_current` against `pages_previous` into
    declining/rising/new groups, sorted by absolute click impact."""

    skip_reason = _quality_gate_reason(pages_current, pages_previous)
    if skip_reason is not None:
        return AlertsResult(skipped_reason=skip_reason)

    previous_by_value = {row.dimension_value: row for row in pages_previous if row.rank is not None}

    declining: list[AlertEntry] = []
    rising: list[AlertEntry] = []
    new: list[AlertEntry] = []

    for row in pages_current:
        if row.rank is None:
            continue  # aggregated top-N remainder ("other"), not one real page/query

        previous_row = previous_by_value.get(row.dimension_value)
        if previous_row is None:
            entry = _new_entry(row, thresholds)
            if entry is not None:
                new.append(entry)
            continue

        entry = _classify(row, previous_row, thresholds)
        if entry is None:
            continue
        (rising if entry.delta_abs > 0 else declining).append(entry)

    declining.sort(key=lambda e: e.delta_abs)
    rising.sort(key=lambda e: -e.delta_abs)
    new.sort(key=lambda e: -e.delta_abs)

    return AlertsResult(
        declining=declining[: thresholds.top_n_per_group],
        rising=rising[: thresholds.top_n_per_group],
        new=new[: thresholds.top_n_per_group],
    )
