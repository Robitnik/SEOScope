from datetime import date
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.schemas.enums import BetterDirection, MetricUnit, PeriodRole, Provider, Quality
from app.schemas.metrics import MetricSpec, MetricValueDTO


def test_metric_spec_defaults():
    spec = MetricSpec(
        key="ahrefs_health_score",
        label="Health Score",
        provider=Provider.ahrefs,
        unit=MetricUnit.score,
        comparison_kind="snapshot",
        better_direction=BetterDirection.up,
    )

    assert spec.precision == 0
    assert spec.allow_derived is False
    assert spec.freshness_sla_hours is None
    assert spec.min_quality_to_show is Quality.partial


def test_metric_spec_rejects_unknown_comparison_kind():
    with pytest.raises(ValidationError):
        MetricSpec(
            key="x",
            label="X",
            provider=Provider.gsc,
            unit=MetricUnit.count,
            comparison_kind="monthly",  # not "range"/"snapshot"
            better_direction=BetterDirection.up,
        )


def test_metric_value_dto_none_is_not_zero():
    value = MetricValueDTO(
        id="mv-1",
        snapshot_id="snap-1",
        client_id="client-1",
        provider=Provider.gsc,
        metric_key="ctr",
        period_role=PeriodRole.current,
        period_start=date(2026, 7, 13),
        period_end=date(2026, 7, 19),
        unit=MetricUnit.percent,
        quality=Quality.unavailable,
    )

    assert value.value_numeric is None


def test_metric_value_dto_keeps_snapshot_role_without_dates():
    value = MetricValueDTO(
        id="mv-2",
        snapshot_id="snap-2",
        client_id="client-1",
        provider=Provider.ahrefs,
        metric_key="health_score",
        period_role=PeriodRole.snapshot,
        value_numeric=Decimal("87"),
        unit=MetricUnit.score,
        quality=Quality.complete,
    )

    assert value.period_start is None
    assert value.period_end is None
    assert value.value_numeric == Decimal("87")
