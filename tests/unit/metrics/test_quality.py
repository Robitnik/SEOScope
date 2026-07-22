from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.metrics.catalog import CATALOG
from app.metrics.quality import assess_quality, compute_delta, ensure_comparable_periods
from app.schemas.enums import PeriodRole, Quality

NOW = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)


# --- assess_quality ----------------------------------------------------


def test_assess_quality_fresh_value_unchanged():
    spec = CATALOG["gsc_clicks"]
    observed_at = NOW - timedelta(hours=1)

    assert assess_quality(spec, Quality.complete, observed_at, NOW) == Quality.complete


def test_assess_quality_stale_when_older_than_sla():
    spec = CATALOG["gsc_clicks"]
    observed_at = NOW - timedelta(hours=spec.freshness_sla_hours + 1)

    assert assess_quality(spec, Quality.complete, observed_at, NOW) == Quality.stale


def test_assess_quality_never_upgrades_unavailable():
    spec = CATALOG["gsc_clicks"]
    observed_at = NOW - timedelta(hours=spec.freshness_sla_hours + 1000)

    assert assess_quality(spec, Quality.unavailable, observed_at, NOW) == Quality.unavailable


def test_assess_quality_no_sla_never_goes_stale():
    spec = CATALOG["gsc_clicks"].model_copy(update={"freshness_sla_hours": None})
    observed_at = NOW - timedelta(days=3650)

    assert assess_quality(spec, Quality.complete, observed_at, NOW) == Quality.complete


# --- compute_delta -------------------------------------------------------


def test_delta_previous_zero_is_null_pct_with_reason_not_infinity():
    spec = CATALOG["gsc_clicks"]

    delta = compute_delta(spec, Decimal("10"), Decimal("0"))

    assert delta.delta_pct is None
    assert delta.reason == "previous_zero"
    assert delta.delta_abs == Decimal("10")


def test_delta_previous_missing():
    spec = CATALOG["gsc_clicks"]

    delta = compute_delta(spec, Decimal("10"), None)

    assert delta.delta_pct is None
    assert delta.reason == "previous_missing"
    assert delta.sentiment == "neutral"


def test_delta_current_missing():
    spec = CATALOG["gsc_clicks"]

    delta = compute_delta(spec, None, Decimal("10"))

    assert delta.delta_abs is None
    assert delta.reason == "current_missing"
    assert delta.sentiment == "neutral"


def test_delta_decrease_in_errors_is_good():
    spec = CATALOG["ahrefs_errors"]  # better_direction=down

    delta = compute_delta(spec, Decimal("5"), Decimal("10"))

    assert delta.delta_abs == Decimal("-5")
    assert delta.sentiment == "good"


def test_delta_decrease_in_clicks_is_bad():
    spec = CATALOG["gsc_clicks"]  # better_direction=up

    delta = compute_delta(spec, Decimal("50"), Decimal("100"))

    assert delta.delta_abs == Decimal("-50")
    assert delta.sentiment == "bad"


def test_delta_neutral_metric_never_signals_good_or_bad():
    spec = CATALOG["ahrefs_urls_crawled"]  # better_direction=neutral

    delta = compute_delta(spec, Decimal("500"), Decimal("100"))

    assert delta.sentiment == "neutral"


def test_delta_zero_change_is_neutral_regardless_of_direction():
    spec = CATALOG["gsc_clicks"]

    delta = compute_delta(spec, Decimal("100"), Decimal("100"))

    assert delta.delta_abs == Decimal("0")
    assert delta.sentiment == "neutral"


def test_delta_pct_rounded_to_two_decimals():
    spec = CATALOG["gsc_clicks"]

    delta = compute_delta(spec, Decimal("110"), Decimal("100"))

    assert delta.delta_pct == Decimal("10.00")


# --- ensure_comparable_periods -----------------------------------------


def test_range_metric_accepts_current_previous_roles():
    spec = CATALOG["gsc_clicks"]
    ensure_comparable_periods(spec, PeriodRole.current, PeriodRole.previous)


def test_range_metric_rejects_snapshot_roles():
    spec = CATALOG["gsc_clicks"]
    with pytest.raises(ValueError):
        ensure_comparable_periods(spec, PeriodRole.snapshot, PeriodRole.snapshot)


def test_snapshot_metric_accepts_snapshot_roles():
    spec = CATALOG["ahrefs_health_score"]
    ensure_comparable_periods(spec, PeriodRole.snapshot, PeriodRole.snapshot)


def test_snapshot_metric_rejects_current_previous_roles():
    spec = CATALOG["ahrefs_health_score"]
    with pytest.raises(ValueError):
        ensure_comparable_periods(spec, PeriodRole.current, PeriodRole.previous)
