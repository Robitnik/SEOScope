"""Composite client health % and dashboard sort keys (PLAN §21.2/§21.3).

Health is a performance signal computed only from services whose collection
is `complete` — the module's central guardrail (PLAN §12): `partial`,
`stale`, `unavailable` or `not_configured` data must never quietly drag the
score down, and zero contributing services renders as "no signal" (`None`),
never as a `0` score.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal

from app.metrics.catalog import CATALOG
from app.schemas.api import (
    ClientCardDTO,
    DashboardSortMode,
    MetricCardValue,
    ProviderSummaryDTO,
)
from app.schemas.enums import BetterDirection, Quality
from app.schemas.metrics import MetricSpec

# Ahrefs Site Health is already a 0..100 "how healthy" score, not a growth
# metric — PLAN §21.2 says it must enter the average directly, not via a
# current/previous delta like every other metric.
_ABSOLUTE_SCORE_METRICS: frozenset[str] = frozenset({"ahrefs_health_score"})

# Relative change beyond this magnitude clamps to the 0/100 edge of the
# scale, so one outlier (e.g. +500% traffic) cannot swing the composite.
_MAX_RELATIVE_CHANGE = Decimal("0.5")

# Tie-break order for the default `quality` sort and its `overall_status`
# component — higher is better. Matches `ClientCardDTO.overall_status`.
_QUALITY_RANK: dict[str, int] = {"ok": 3, "stale": 2, "attention": 1, "failed_all": 0}


@dataclass(frozen=True)
class HealthResult:
    """`value` is `None` whenever `contributing == 0` — never `0`."""

    value: Decimal | None
    contributing: int
    configured: int


def _score_metric(spec: MetricSpec, metric: MetricCardValue) -> Decimal | None:
    """0..100 score for one metric, 50 == no change; `None` if unscorable."""
    if metric.metric_key in _ABSOLUTE_SCORE_METRICS:
        if metric.current_value is None:
            return None
        return max(Decimal(0), min(Decimal(100), metric.current_value))

    if (
        metric.current_value is None
        or metric.previous_value is None
        or metric.previous_value == 0
    ):
        return None

    relative_change = (metric.current_value - metric.previous_value) / abs(metric.previous_value)
    if spec.better_direction == BetterDirection.down:
        relative_change = -relative_change
    clamped = max(-_MAX_RELATIVE_CHANGE, min(_MAX_RELATIVE_CHANGE, relative_change))
    return Decimal(50) + clamped * Decimal(100)


def _scorable(
    metric: MetricCardValue, catalog: Mapping[str, MetricSpec]
) -> tuple[MetricSpec, Decimal] | None:
    if metric.quality != Quality.complete:
        return None
    spec = catalog.get(metric.metric_key)
    if spec is None or spec.better_direction == BetterDirection.neutral:
        return None
    score = _score_metric(spec, metric)
    if score is None:
        return None
    return spec, score


def compute_health(
    summaries: Sequence[ProviderSummaryDTO],
    catalog: Mapping[str, MetricSpec] = CATALOG,
) -> HealthResult:
    """`value = None` (never `0`) when no service contributed."""
    configured = sum(1 for summary in summaries if summary.state != "not_configured")

    sub_scores: list[tuple[Decimal, Decimal]] = []  # (sub_score, service_weight)
    for summary in summaries:
        if summary.state != "complete":
            continue
        scored = [
            item
            for metric in summary.metrics
            if (item := _scorable(metric, catalog)) is not None
        ]
        if not scored:
            continue
        scores = [score for _, score in scored]
        weights = [Decimal(str(spec.health_weight)) for spec, _ in scored]
        sub_score = sum(scores, Decimal(0)) / len(scores)
        service_weight = sum(weights, Decimal(0)) / len(weights)
        sub_scores.append((sub_score, service_weight))

    if not sub_scores:
        return HealthResult(value=None, contributing=0, configured=configured)

    total_weight = sum((weight for _, weight in sub_scores), Decimal(0))
    if total_weight > 0:
        value = sum((score * weight for score, weight in sub_scores), Decimal(0)) / total_weight
    else:
        value = sum((score for score, _ in sub_scores), Decimal(0)) / len(sub_scores)

    return HealthResult(
        value=value.quantize(Decimal("1")),
        contributing=len(sub_scores),
        configured=configured,
    )


def _name_key(card: ClientCardDTO) -> str:
    return card.name.casefold()


def _quality_rank(card: ClientCardDTO) -> int:
    return _QUALITY_RANK.get(card.overall_status, 0)


def _coverage_ratio(card: ClientCardDTO) -> Decimal:
    if card.health.coverage_configured == 0:
        return Decimal(0)
    return Decimal(card.health.coverage_contributing) / Decimal(card.health.coverage_configured)


def _freshness_key(card: ClientCardDTO) -> tuple[int, float]:
    """Ascending key: most recent first, `None` (never synced) always last."""
    if card.last_synced_at is None:
        return (1, 0.0)
    return (0, -card.last_synced_at.timestamp())


def _worst_metric_score(
    card: ClientCardDTO, catalog: Mapping[str, MetricSpec]
) -> Decimal | None:
    worst: Decimal | None = None
    for summary in card.providers:
        for metric in summary.metrics:
            scored = _scorable(metric, catalog)
            if scored is None:
                continue
            _, score = scored
            if worst is None or score < worst:
                worst = score
    return worst


def sort_clients(
    cards: Sequence[ClientCardDTO],
    mode: DashboardSortMode = "quality",
    *,
    catalog: Mapping[str, MetricSpec] = CATALOG,
) -> list[ClientCardDTO]:
    """Deterministic, stable order for the dashboard (PLAN §21.3).

    `quality` (default): `coverage ↓ → quality rank ↓ → freshness ↓ →
    name ↑` — clients with no contributing services fall out naturally
    because their coverage ratio is the lowest possible value (0).
    """
    if mode == "name":
        return sorted(cards, key=_name_key)

    if mode == "last_sync":
        return sorted(cards, key=lambda c: (_freshness_key(c), _name_key(c)))

    if mode == "health":

        def _health_key(card: ClientCardDTO) -> tuple[int, Decimal]:
            value = card.health.value
            return (0, -value) if value is not None else (1, Decimal(0))

        return sorted(cards, key=lambda c: (_health_key(c), _name_key(c)))

    if mode == "biggest_drop":

        def _drop_key(card: ClientCardDTO) -> tuple[int, Decimal]:
            worst = _worst_metric_score(card, catalog)
            return (0, worst) if worst is not None else (1, Decimal(0))

        return sorted(cards, key=lambda c: (_drop_key(c), _name_key(c)))

    return sorted(
        cards,
        key=lambda c: (
            -_coverage_ratio(c),
            -_quality_rank(c),
            _freshness_key(c),
            _name_key(c),
        ),
    )
