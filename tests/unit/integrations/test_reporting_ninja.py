"""Tests for the Reporting Ninja (GA4) `ProviderAdapter` (task-09).

`seo_audit.dashboard.reporting_ninja.collect_ga4` is monkeypatched throughout — these
tests cover only this module's own mapping/business-rule logic (canonical key mapping,
per-period unavailable synthesis, quality passthrough), not the AISeoTools wrapper itself
(covered by AISeoTools' own `tests/dashboard/test_reporting_ninja.py`).
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from seo_audit.dashboard.events import ProgressEvent as DashboardProgressEvent
from seo_audit.dashboard.models import DashboardMetricValue
from seo_audit.dashboard.models import DashboardResult as DashResult
from seo_audit.dashboard.models import DateRange as DashDateRange
from seo_audit.dashboard.models import MetricUnit as DashUnit
from seo_audit.dashboard.models import PeriodRole as DashPeriodRole
from seo_audit.dashboard.models import Provider as DashProvider
from seo_audit.dashboard.models import Quality as DashQuality

from app.integrations import reporting_ninja as adapter_module
from app.schemas.adapter import CollectionContext, DateRange
from app.schemas.enums import ComparisonMode, PeriodKind, PeriodRole, Provider, Quality, SourceMode

CURRENT = DateRange(start=date(2026, 6, 1), end=date(2026, 6, 30))
PREVIOUS = DateRange(start=date(2026, 5, 1), end=date(2026, 5, 31))


def _context(**overrides) -> CollectionContext:
    defaults = dict(
        client_id="client-1",
        provider=Provider.reporting_ninja,
        source_mode=SourceMode.api,
        period_kind=PeriodKind.month,
        comparison_mode=ComparisonMode.closed,
        current=CURRENT,
        previous=PREVIOUS,
        timezone="America/New_York",
        integration_config={"connection_key": "seo@example.com", "account_id": "properties/1"},
    )
    defaults.update(overrides)
    return CollectionContext(**defaults)


def _dash_value(
    key: str, role: DashPeriodRole, value: int | None, date_range: DashDateRange
) -> DashboardMetricValue:
    return DashboardMetricValue(
        metric_key=key,
        period_role=role,
        period_start=date_range.start,
        period_end=date_range.end,
        value_numeric=Decimal(value) if value is not None else None,
        unit=DashUnit.count,
    )


def _dash_result(*, quality=DashQuality.complete, both_periods=True, warnings=None) -> DashResult:
    current = DashDateRange(start=CURRENT.start, end=CURRENT.end)
    previous = DashDateRange(start=PREVIOUS.start, end=PREVIOUS.end)
    values = [
        _dash_value("users", DashPeriodRole.current, 100, current),
        _dash_value("organic_users", DashPeriodRole.current, 40, current),
        _dash_value("total_conversions", DashPeriodRole.current, 10, current),
        _dash_value("organic_conversions", DashPeriodRole.current, 4, current),
    ]
    if both_periods:
        values += [
            _dash_value("users", DashPeriodRole.previous, 90, previous),
            _dash_value("organic_users", DashPeriodRole.previous, 30, previous),
            _dash_value("total_conversions", DashPeriodRole.previous, 8, previous),
            _dash_value("organic_conversions", DashPeriodRole.previous, 3, previous),
        ]
    return DashResult(
        provider=DashProvider.reporting_ninja,
        quality=quality,
        observed_at=datetime.now(UTC),
        current=current,
        previous=previous,
        values=values,
        warnings=warnings or [],
        provenance={"source_mode": "api", "transport": "reporting_ninja"},
    )


class _NoCancel:
    def __call__(self) -> bool:
        return False


# ── preflight ────────────────────────────────────────────────────────────────
def test_preflight_missing_config_is_not_configured():
    adapter = adapter_module.ReportingNinjaAdapter()
    result = adapter.preflight(_context(integration_config={}))
    assert result.ok is False
    assert result.reason_code == "not_configured"


def test_preflight_missing_account_id_only_is_not_configured():
    adapter = adapter_module.ReportingNinjaAdapter()
    result = adapter.preflight(_context(integration_config={"connection_key": "x"}))
    assert result.ok is False
    assert result.reason_code == "not_configured"


def test_preflight_no_api_key_is_auth_required(monkeypatch):
    monkeypatch.delenv("REPORTING_NINJA_API_KEY", raising=False)
    adapter = adapter_module.ReportingNinjaAdapter()
    result = adapter.preflight(_context())
    assert result.ok is False
    assert result.reason_code == "auth_required"


def test_preflight_with_api_key_is_ok(monkeypatch):
    monkeypatch.setenv("REPORTING_NINJA_API_KEY", "secret-key")
    adapter = adapter_module.ReportingNinjaAdapter()
    result = adapter.preflight(_context())
    assert result.ok is True


# ── collect: happy path ──────────────────────────────────────────────────────
def test_collect_maps_to_canonical_catalog_keys(monkeypatch):
    # The adapter imports `collect_ga4` lazily from the real wrapper module inside
    # `_collect`, so patching it there (rather than on `adapter_module`) is what actually
    # takes effect at call time.
    from seo_audit.dashboard import reporting_ninja as real_wrapper

    monkeypatch.setattr(real_wrapper, "collect_ga4", lambda *a, **k: _dash_result())

    adapter = adapter_module.ReportingNinjaAdapter()
    result = adapter.collect(_context(), lambda e: None, _NoCancel())

    by_key = {(m.metric_key, m.period_role): m for m in result.metrics}
    assert by_key[("rn_users", PeriodRole.current)].value_numeric == Decimal(100)
    assert by_key[("rn_organic_users", PeriodRole.current)].value_numeric == Decimal(40)
    assert by_key[("rn_total_conversions", PeriodRole.current)].value_numeric == Decimal(10)
    assert by_key[("rn_organic_conversions", PeriodRole.current)].value_numeric == Decimal(4)
    assert result.quality == Quality.complete
    assert len(result.metrics) == 8  # 4 metrics x 2 periods


def test_collect_one_period_missing_is_unavailable_not_zero(monkeypatch):
    from seo_audit.dashboard import reporting_ninja as real_wrapper

    monkeypatch.setattr(
        real_wrapper, "collect_ga4",
        lambda *a, **k: _dash_result(quality=DashQuality.partial, both_periods=False),
    )
    adapter = adapter_module.ReportingNinjaAdapter()

    result = adapter.collect(_context(), lambda e: None, _NoCancel())

    by_key = {(m.metric_key, m.period_role): m for m in result.metrics}
    previous_entry = by_key[("rn_users", PeriodRole.previous)]
    assert previous_entry.value_numeric is None
    assert previous_entry.quality == Quality.unavailable
    current_entry = by_key[("rn_users", PeriodRole.current)]
    assert current_entry.value_numeric == Decimal(100)
    assert current_entry.quality == Quality.partial
    assert result.quality == Quality.partial


def test_collect_discovery_failure_propagates_unavailable(monkeypatch):
    from seo_audit.dashboard import reporting_ninja as real_wrapper

    monkeypatch.setattr(
        real_wrapper, "collect_ga4",
        lambda *a, **k: _dash_result(quality=DashQuality.unavailable, both_periods=False)
        .model_copy(update={"values": [], "error_code": "ga4_discovery_failed"}),
    )
    adapter = adapter_module.ReportingNinjaAdapter()

    result = adapter.collect(_context(), lambda e: None, _NoCancel())

    assert result.quality == Quality.unavailable
    assert result.error_code == "ga4_discovery_failed"
    assert all(m.value_numeric is None and m.quality == Quality.unavailable for m in result.metrics)
    assert len(result.metrics) == 8


def test_collect_unexpected_exception_never_raises(monkeypatch):
    from seo_audit.dashboard import reporting_ninja as real_wrapper

    def _boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(real_wrapper, "collect_ga4", _boom)
    adapter = adapter_module.ReportingNinjaAdapter()

    result = adapter.collect(_context(), lambda e: None, _NoCancel())

    assert result.quality == Quality.unavailable
    assert result.error_code == "adapter_error"
    assert result.metrics == []


# ── event bridging ───────────────────────────────────────────────────────────
def test_bridge_emit_translates_dashboard_event_to_app_event():
    events = []
    on_event = adapter_module._bridge_emit(events.append)

    on_event(
        DashboardProgressEvent(
            provider=DashProvider.reporting_ninja, stage="fetch_current",
            message="hello", details={"x": 1},
        )
    )

    assert len(events) == 1
    assert events[0].provider == Provider.reporting_ninja
    assert events[0].stage == "fetch_current"
    assert events[0].message == "hello"
    assert events[0].details == {"x": 1}


def test_bridge_emit_never_raises_on_broken_callback():
    def _broken(_event) -> None:
        raise RuntimeError("ui callback exploded")

    on_event = adapter_module._bridge_emit(_broken)
    # must not raise
    on_event(DashboardProgressEvent(provider=DashProvider.reporting_ninja, stage="discover"))
