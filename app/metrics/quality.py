"""Freshness, delta and range/snapshot comparability rules (PLAN §12).

Delta semantics are the crux of this module: sentiment ("good"/"bad") comes
from `MetricSpec.better_direction`, never from the sign of the change — a
drop in Ahrefs Errors is `good`, a drop in Clicks is `bad`. A `None`
previous value never produces an `infinity` percentage; it produces
`delta_pct=None` plus a `reason` the API/frontend can render as a badge.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict

from app.schemas.enums import BetterDirection, PeriodRole, Quality
from app.schemas.metrics import MetricSpec

Sentiment = Literal["good", "bad", "neutral"]


class DeltaDTO(BaseModel):
    """Change between two already-normalized values for one metric."""

    model_config = ConfigDict(extra="forbid")

    delta_abs: Decimal | None
    delta_pct: Decimal | None
    reason: str | None
    sentiment: Sentiment


def assess_quality(
    spec: MetricSpec,
    quality: Quality,
    observed_at: datetime | None,
    now: datetime,
) -> Quality:
    """Layer freshness on top of an already-assessed `Quality`.

    Only ever downgrades towards `stale`; never upgrades a quality and never
    touches `unavailable` (there is nothing to be "fresh" about there).
    """
    if quality == Quality.unavailable:
        return quality
    if spec.freshness_sla_hours is None or observed_at is None:
        return quality
    age_hours = (now - observed_at).total_seconds() / 3600
    if age_hours > spec.freshness_sla_hours:
        return Quality.stale
    return quality


def ensure_comparable_periods(
    spec: MetricSpec, current_role: PeriodRole, previous_role: PeriodRole
) -> None:
    """Reject comparing a `range` metric against a `snapshot` one or vice versa.

    `comparison_kind="range"` metrics must compare `current` against
    `previous`; `comparison_kind="snapshot"` metrics compare the last valid
    snapshot of each period, both tagged `PeriodRole.snapshot` by the
    repository. Mixing these would silently compare incompatible periods
    (PLAN §12).
    """
    if spec.comparison_kind == "range":
        if current_role != PeriodRole.current or previous_role != PeriodRole.previous:
            raise ValueError(
                f"range metric {spec.key!r} requires current/previous period roles, "
                f"got {current_role!r}/{previous_role!r}"
            )
    else:
        if current_role != PeriodRole.snapshot or previous_role != PeriodRole.snapshot:
            raise ValueError(
                f"snapshot metric {spec.key!r} requires snapshot period roles on both sides, "
                f"got {current_role!r}/{previous_role!r}"
            )


def compute_delta(
    spec: MetricSpec, current: Decimal | None, previous: Decimal | None
) -> DeltaDTO:
    """Compute `delta_abs`/`delta_pct`/`sentiment` for one metric.

    `previous == 0` (or missing) never yields `delta_pct=infinity`; it
    yields `None` plus a stable `reason` string.
    """
    if current is None:
        return DeltaDTO(
            delta_abs=None, delta_pct=None, reason="current_missing", sentiment="neutral"
        )
    if previous is None:
        return DeltaDTO(
            delta_abs=None, delta_pct=None, reason="previous_missing", sentiment="neutral"
        )

    delta_abs = current - previous
    if previous == 0:
        delta_pct: Decimal | None = None
        reason: str | None = "previous_zero"
    else:
        delta_pct = ((delta_abs / previous) * Decimal(100)).quantize(Decimal("0.01"))
        reason = None

    if delta_abs == 0 or spec.better_direction == BetterDirection.neutral:
        sentiment: Sentiment = "neutral"
    elif spec.better_direction == BetterDirection.up:
        sentiment = "good" if delta_abs > 0 else "bad"
    else:  # BetterDirection.down
        sentiment = "good" if delta_abs < 0 else "bad"

    return DeltaDTO(delta_abs=delta_abs, delta_pct=delta_pct, reason=reason, sentiment=sentiment)
