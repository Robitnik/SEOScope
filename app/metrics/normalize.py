"""Normalize adapter output to canonical precision + derived metrics (PLAN §12).

`normalize_result` never touches I/O or the DB — it takes the
`CollectionResult` an adapter already returned and:

1. rounds each metric's `value_numeric` to its catalog `precision`
   (`None` stays `None`; a missing value is never rounded into `0`);
2. adds derived metrics (e.g. `gsc_ctr = gsc_clicks / gsc_impressions`)
   registered in `catalog.DERIVED_RATIO_RULES`, but only when both
   components are present for the same `period_role` with
   `quality == complete` — a truncated or partial component must never be
   silently promoted into a derived total.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from app.metrics.catalog import CATALOG, DERIVED_RATIO_RULES
from app.schemas.adapter import CollectionResult, MetricValueResult
from app.schemas.enums import PeriodRole, Quality
from app.schemas.metrics import MetricSpec


def _round(value: Decimal | None, precision: int) -> Decimal | None:
    if value is None:
        return None
    quantum = Decimal(1).scaleb(-precision)
    return value.quantize(quantum, rounding=ROUND_HALF_UP)


def _rounded_copy(value: MetricValueResult, spec: MetricSpec | None) -> MetricValueResult:
    if spec is None or value.value_numeric is None:
        return value
    return value.model_copy(update={"value_numeric": _round(value.value_numeric, spec.precision)})


def _compute_derived(
    metrics: list[MetricValueResult], catalog: dict[str, MetricSpec]
) -> list[MetricValueResult]:
    by_key_role: dict[tuple[str, PeriodRole], MetricValueResult] = {
        (m.metric_key, m.period_role): m for m in metrics
    }
    derived: list[MetricValueResult] = []

    for derived_key, (numerator_key, denominator_key) in DERIVED_RATIO_RULES.items():
        spec = catalog.get(derived_key)
        if spec is None or not spec.allow_derived:
            continue
        for role in (PeriodRole.current, PeriodRole.previous):
            if (derived_key, role) in by_key_role:
                continue  # adapter already supplied it directly
            numerator = by_key_role.get((numerator_key, role))
            denominator = by_key_role.get((denominator_key, role))
            if numerator is None or denominator is None:
                continue
            if numerator.quality != Quality.complete or denominator.quality != Quality.complete:
                continue
            if numerator.value_numeric is None or denominator.value_numeric is None:
                continue
            if denominator.value_numeric == 0:
                continue
            ratio = (numerator.value_numeric / denominator.value_numeric) * Decimal(100)
            derived.append(
                MetricValueResult(
                    metric_key=derived_key,
                    period_role=role,
                    period_start=numerator.period_start,
                    period_end=numerator.period_end,
                    value_numeric=_round(ratio, spec.precision),
                    unit=spec.unit,
                    quality=Quality.complete,
                    is_derived=True,
                )
            )
    return derived


def normalize_result(
    result: CollectionResult, catalog: dict[str, MetricSpec] = CATALOG
) -> list[MetricValueResult]:
    """Canonicalize precision on `result.metrics` and add derived metrics."""
    rounded = [_rounded_copy(m, catalog.get(m.metric_key)) for m in result.metrics]
    return rounded + _compute_derived(rounded, catalog)
