from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.schemas.adapter import (
    CollectionContext,
    CollectionResult,
    DateRange,
    MetricValueResult,
    PreflightResult,
    ProgressEvent,
    RawArtifactRef,
)
from app.schemas.enums import (
    ComparisonMode,
    MetricUnit,
    PeriodKind,
    PeriodRole,
    Provider,
    Quality,
    SourceMode,
)


def _context(**overrides):
    defaults = dict(
        client_id="client-1",
        provider=Provider.gsc,
        source_mode=SourceMode.api,
        period_kind=PeriodKind.week,
        comparison_mode=ComparisonMode.closed,
        current=DateRange(start=date(2026, 7, 13), end=date(2026, 7, 19)),
        previous=DateRange(start=date(2026, 7, 6), end=date(2026, 7, 12)),
        timezone="America/New_York",
    )
    defaults.update(overrides)
    return CollectionContext(**defaults)


def test_progress_event_defaults():
    event = ProgressEvent(provider=Provider.ahrefs, stage="preflight")

    assert event.level == "info"
    assert event.message == ""
    assert event.details == {}
    assert event.completed is None
    assert event.percent is None


def test_progress_event_rejects_unknown_fields():
    with pytest.raises(ValidationError):
        ProgressEvent(provider=Provider.ahrefs, stage="preflight", unexpected="x")


def test_date_range_is_inclusive_pair():
    date_range = DateRange(start=date(2026, 1, 1), end=date(2026, 1, 31))

    assert date_range.start == date(2026, 1, 1)
    assert date_range.end == date(2026, 1, 31)


def test_collection_context_round_trip():
    context = _context(force_refresh=True, integration_config={"project_id": "abc"})

    assert context.force_refresh is True
    assert context.integration_config == {"project_id": "abc"}
    assert context.session_paths == {}


def test_metric_value_result_none_is_not_zero():
    result = MetricValueResult(
        metric_key="clicks",
        period_role=PeriodRole.current,
        unit=MetricUnit.count,
        quality=Quality.unavailable,
    )

    assert result.value_numeric is None
    assert result.value_text is None


def test_metric_value_result_keeps_decimal_precision():
    result = MetricValueResult(
        metric_key="avg_position",
        period_role=PeriodRole.current,
        unit=MetricUnit.position,
        quality=Quality.complete,
        value_numeric=Decimal("4.37"),
    )

    assert result.value_numeric == Decimal("4.37")
    assert isinstance(result.value_numeric, Decimal)


def test_raw_artifact_ref_requires_relative_path_field():
    artifact = RawArtifactRef(
        kind="json", path="ahrefs/2026-07-19/findings.json", sha256="deadbeef"
    )

    assert artifact.mime is None
    assert artifact.size_bytes is None


def test_collection_result_never_raises_shape_supports_partial_quality():
    result = CollectionResult(
        provider=Provider.semrush,
        quality=Quality.partial,
        observed_at=datetime(2026, 7, 19, 12, 0, tzinfo=UTC),
        metrics=[],
        warnings=["manifest incomplete"],
        error_code=None,
    )

    assert result.quality is Quality.partial
    assert result.warnings == ["manifest incomplete"]
    assert result.error_code is None


def test_preflight_result_not_ok_carries_reason_code():
    preflight = PreflightResult(
        provider=Provider.reporting_ninja,
        ok=False,
        reason_code="not_configured",
    )

    assert preflight.ok is False
    assert preflight.reason_code == "not_configured"
