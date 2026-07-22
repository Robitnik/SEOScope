"""Metric catalog record type and persisted-value DTO (PLAN §3.2, §6).

`MetricSpec` is the type of one catalog row; `task-04` owns the actual
`CATALOG: dict[str, MetricSpec]` content. `MetricValueDTO` mirrors the
`metric_values` table and is what repositories/API read back after
persistence — distinct from `MetricValueResult` (`app.schemas.adapter`),
which is what an adapter returns *before* it has been persisted and has no
`id`/`snapshot_id` yet.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict

from app.schemas.enums import BetterDirection, MetricUnit, PeriodRole, Provider, Quality


class MetricSpec(BaseModel):
    """One catalog row: how a metric is measured, compared and displayed."""

    model_config = ConfigDict(extra="forbid")

    key: str
    label: str  # Ukrainian UI label
    provider: Provider
    unit: MetricUnit
    comparison_kind: Literal["range", "snapshot"]
    better_direction: BetterDirection
    precision: int = 0
    allow_derived: bool = False
    freshness_sla_hours: int | None = None
    min_quality_to_show: Quality = Quality.partial
    # Weight of this metric's provider in the composite health % (task-15,
    # PLAN §21.2). MVP starts every metric equal; a later product decision
    # can differentiate providers/metrics without a shape change.
    health_weight: float = 1.0


class MetricValueDTO(BaseModel):
    """Persisted metric value, mirrors the `metric_values` table (PLAN §6)."""

    model_config = ConfigDict(extra="forbid")

    id: str
    snapshot_id: str
    client_id: str
    provider: Provider
    metric_key: str
    period_role: PeriodRole
    period_start: date | None = None  # None for snapshot metrics
    period_end: date | None = None
    value_numeric: Decimal | None = None  # None means "no data", never 0
    value_text: str | None = None
    unit: MetricUnit
    quality: Quality
    is_derived: bool = False
