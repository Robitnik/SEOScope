from app.schemas.enums import (
    STAGE_ORDER,
    BetterDirection,
    CellStatus,
    ComparisonMode,
    JobStatus,
    MetricUnit,
    PeriodKind,
    PeriodRole,
    Provider,
    Quality,
    SourceMode,
    StageStatus,
)


def test_provider_values_are_stable():
    assert {p.value for p in Provider} == {"ahrefs", "semrush", "gsc", "reporting_ninja"}


def test_period_kind_values():
    # Extended additively by task-14 (PLAN §21.1): week/month keep their
    # meaning, quarter/year/custom are served by aggregating stored history.
    assert {p.value for p in PeriodKind} == {"week", "month", "quarter", "year", "custom"}


def test_comparison_mode_values():
    assert {c.value for c in ComparisonMode} == {"closed", "to_date"}


def test_period_role_values():
    assert {r.value for r in PeriodRole} == {"current", "previous", "snapshot"}


def test_quality_values():
    assert {q.value for q in Quality} == {
        "complete",
        "partial",
        "truncated",
        "stale",
        "unavailable",
    }


def test_better_direction_values():
    assert {b.value for b in BetterDirection} == {"up", "down", "neutral"}


def test_source_mode_values():
    assert {s.value for s in SourceMode} == {"auto", "api", "browser", "pdf"}


def test_job_status_values():
    assert {j.value for j in JobStatus} == {
        "queued",
        "running",
        "succeeded",
        "partial",
        "failed",
        "cancel_requested",
        "cancelled",
    }


def test_stage_status_values():
    assert {s.value for s in StageStatus} == {
        "pending",
        "running",
        "succeeded",
        "failed",
        "skipped",
    }


def test_cell_status_values_distinguish_not_configured_from_failure_states():
    values = {c.value for c in CellStatus}
    assert values == {
        "queued",
        "running",
        "succeeded",
        "partial",
        "failed",
        "failed_interrupted",
        "not_configured",
        "awaiting_auth",
        "skipped",
        "cancel_requested",
        "cancelled",
    }
    # not_configured must never collapse into failed — the whole point of the
    # state (PLAN §9.2).
    assert CellStatus.not_configured != CellStatus.failed


def test_metric_unit_values():
    assert {m.value for m in MetricUnit} == {
        "count",
        "percent",
        "score",
        "position",
        "currency",
        "ratio",
    }


def test_stage_order_is_the_documented_sequence():
    assert STAGE_ORDER == (
        "queued",
        "preflight",
        "authenticate",
        "fetch_current",
        "fetch_previous",
        "normalize",
        "validate",
        "persist",
        "complete",
    )
