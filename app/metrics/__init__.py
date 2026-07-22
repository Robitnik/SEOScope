"""Metrics engine (PLAN §3.1/§3.2/§12): periods, catalog, normalize, quality.

Pure, side-effect-free library — no DB access, no collection, no rendering.
Import from here, not from the submodules directly.
"""

from __future__ import annotations

from app.metrics.aggregate import (
    WEIGHTED_AVERAGE_RULES,
    StoredMetricValue,
    aggregate_metrics,
)
from app.metrics.catalog import CATALOG, DERIVED_RATIO_RULES, metrics_for
from app.metrics.health import HealthResult, compute_health, sort_clients
from app.metrics.normalize import normalize_result
from app.metrics.periods import Periods, resolve_periods
from app.metrics.quality import DeltaDTO, assess_quality, compute_delta, ensure_comparable_periods

__all__ = [
    "CATALOG",
    "DERIVED_RATIO_RULES",
    "WEIGHTED_AVERAGE_RULES",
    "DeltaDTO",
    "HealthResult",
    "Periods",
    "StoredMetricValue",
    "aggregate_metrics",
    "assess_quality",
    "compute_delta",
    "compute_health",
    "ensure_comparable_periods",
    "metrics_for",
    "normalize_result",
    "resolve_periods",
    "sort_clients",
]
