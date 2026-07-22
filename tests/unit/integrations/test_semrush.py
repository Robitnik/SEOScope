"""Tests for the Semrush `ProviderAdapter` (task-11).

`seo_audit.dashboard.semrush.collect_semrush_browser`/`collect_semrush_api` are monkeypatched
throughout — these tests cover only this module's own mapping/policy logic (browser-first with
an explicit-policy API fallback, quality mapping, units-exhausted handling), not the AISeoTools
wrapper itself (covered by AISeoTools' own `tests/dashboard/test_semrush.py`).
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from seo_audit.dashboard.events import ProgressEvent as DashboardProgressEvent
from seo_audit.dashboard.models import DashboardMetricValue
from seo_audit.dashboard.models import DashboardResult as DashResult
from seo_audit.dashboard.models import MetricUnit as DashUnit
from seo_audit.dashboard.models import PeriodRole as DashPeriodRole
from seo_audit.dashboard.models import Provider as DashProvider
from seo_audit.dashboard.models import Quality as DashQuality
from seo_audit.units_fallback import UnitsExhausted

from app.integrations import semrush as adapter_module
from app.jobs.fake_adapter import CancelledByAdapter
from app.schemas.adapter import CollectionContext, DateRange
from app.schemas.enums import ComparisonMode, PeriodKind, Provider, Quality, SourceMode


def _context(**overrides) -> CollectionContext:
    defaults = dict(
        client_id="client-1",
        provider=Provider.semrush,
        source_mode=SourceMode.auto,
        period_kind=PeriodKind.week,
        comparison_mode=ComparisonMode.closed,
        current=DateRange(start=date(2026, 7, 13), end=date(2026, 7, 19)),
        previous=DateRange(start=date(2026, 7, 6), end=date(2026, 7, 12)),
        timezone="America/New_York",
        integration_config={"domain": "acme.com"},
    )
    defaults.update(overrides)
    return CollectionContext(**defaults)


def _dash_value(key: str, value, unit=DashUnit.count) -> DashboardMetricValue:
    return DashboardMetricValue(
        metric_key=key, period_role=DashPeriodRole.snapshot,
        value_numeric=Decimal(value) if value is not None else None, unit=unit,
    )


def _dash_result(
    *, quality=DashQuality.complete, authority=61, traffic=3400, keywords=1200,
    refdomains=210, warnings=None, provenance=None, units_spent=None,
) -> DashResult:
    return DashResult(
        provider=DashProvider.semrush,
        quality=quality,
        observed_at=datetime.now(UTC),
        values=[
            _dash_value("semrush_authority_score", authority, unit=DashUnit.score),
            _dash_value("semrush_organic_traffic", traffic),
            _dash_value("semrush_organic_keywords", keywords),
            _dash_value("semrush_referring_domains", refdomains),
        ],
        warnings=warnings or [],
        provenance=provenance or {"source_mode": "browser"},
        units_spent=units_spent,
    )


class _NoCancel:
    def __call__(self) -> bool:
        return False


class _AlwaysCancel:
    def __call__(self) -> bool:
        return True


# ── preflight ────────────────────────────────────────────────────────────────

def test_preflight_missing_domain_is_not_configured():
    adapter = adapter_module.SemrushAdapter()
    result = adapter.preflight(_context(integration_config={}))
    assert result.ok is False
    assert result.reason_code == "not_configured"


def test_preflight_pdf_source_mode_unsupported():
    adapter = adapter_module.SemrushAdapter()
    result = adapter.preflight(_context(source_mode=SourceMode.pdf))
    assert result.ok is False
    assert result.reason_code == "not_configured"


def test_preflight_browser_mode_without_profile_is_not_configured(monkeypatch):
    monkeypatch.setattr(adapter_module, "_browser_profile_ready", lambda profile: False)
    adapter = adapter_module.SemrushAdapter()
    result = adapter.preflight(_context(source_mode=SourceMode.browser))
    assert result.ok is False
    assert result.reason_code == "not_configured"


def test_preflight_browser_mode_with_profile_is_ok(monkeypatch):
    monkeypatch.setattr(adapter_module, "_browser_profile_ready", lambda profile: True)
    adapter = adapter_module.SemrushAdapter()
    result = adapter.preflight(_context(source_mode=SourceMode.browser))
    assert result.ok is True
    assert result.units_estimated == 0


def test_preflight_api_mode_without_key_is_auth_required(monkeypatch):
    monkeypatch.delenv(adapter_module.SEMRUSH_API_KEY_ENV, raising=False)
    adapter = adapter_module.SemrushAdapter()
    result = adapter.preflight(_context(source_mode=SourceMode.api))
    assert result.ok is False
    assert result.reason_code == "auth_required"


def test_preflight_api_mode_with_key_is_ok(monkeypatch):
    monkeypatch.setenv(adapter_module.SEMRUSH_API_KEY_ENV, "secret-key")
    adapter = adapter_module.SemrushAdapter()
    result = adapter.preflight(_context(source_mode=SourceMode.api))
    assert result.ok is True
    assert result.units_estimated == adapter_module._API_UNITS_ESTIMATE


def test_preflight_auto_prefers_browser_when_ready(monkeypatch):
    monkeypatch.setattr(adapter_module, "_browser_profile_ready", lambda profile: True)
    monkeypatch.delenv(adapter_module.SEMRUSH_API_KEY_ENV, raising=False)
    adapter = adapter_module.SemrushAdapter()
    result = adapter.preflight(_context(source_mode=SourceMode.auto))
    assert result.ok is True
    assert result.units_estimated == 0


def test_preflight_auto_without_browser_and_without_budget_is_not_configured(monkeypatch):
    monkeypatch.setattr(adapter_module, "_browser_profile_ready", lambda profile: False)
    monkeypatch.setenv(adapter_module.SEMRUSH_API_KEY_ENV, "secret-key")
    adapter = adapter_module.SemrushAdapter()
    context = _context(source_mode=SourceMode.auto, integration_config={"domain": "acme.com"})
    result = adapter.preflight(context)
    # No `unit_budget` -> the metered API fallback is NOT an explicit policy opt-in yet.
    assert result.ok is False
    assert result.reason_code == "not_configured"


def test_preflight_auto_without_browser_but_with_budget_allows_api(monkeypatch):
    monkeypatch.setattr(adapter_module, "_browser_profile_ready", lambda profile: False)
    monkeypatch.setenv(adapter_module.SEMRUSH_API_KEY_ENV, "secret-key")
    adapter = adapter_module.SemrushAdapter()
    context = _context(
        source_mode=SourceMode.auto,
        integration_config={"domain": "acme.com", "unit_budget": 500},
    )
    result = adapter.preflight(context)
    assert result.ok is True
    assert result.units_estimated == adapter_module._API_UNITS_ESTIMATE


# ── collect: browser happy path ─────────────────────────────────────────────

def _patch_browser(monkeypatch, result: DashResult | None):
    from seo_audit.dashboard import semrush as real_wrapper

    monkeypatch.setattr(adapter_module, "_browser_profile_ready", lambda profile: True)
    monkeypatch.setattr(real_wrapper, "collect_semrush_browser", lambda *a, **k: result)


def _patch_api(monkeypatch, result_or_raise):
    from seo_audit.dashboard import semrush as real_wrapper

    if isinstance(result_or_raise, BaseException) or (
        isinstance(result_or_raise, type) and issubclass(result_or_raise, BaseException)
    ):
        def _raise(*a, **k):
            raise result_or_raise

        monkeypatch.setattr(real_wrapper, "collect_semrush_api", _raise)
    else:
        monkeypatch.setattr(real_wrapper, "collect_semrush_api", lambda *a, **k: result_or_raise)


def test_collect_browser_complete_maps_all_four_metrics(monkeypatch):
    _patch_browser(monkeypatch, _dash_result())
    adapter = adapter_module.SemrushAdapter()

    result = adapter.collect(_context(), lambda e: None, _NoCancel())

    by_key = {m.metric_key: m for m in result.metrics}
    assert by_key["semrush_authority_score"].value_numeric == Decimal(61)
    assert by_key["semrush_organic_traffic"].value_numeric == Decimal(3400)
    assert by_key["semrush_organic_keywords"].value_numeric == Decimal(1200)
    assert by_key["semrush_referring_domains"].value_numeric == Decimal(210)
    assert result.quality == Quality.complete
    assert result.provenance["source_used"] == "browser"


def test_collect_browser_missing_value_is_unavailable_not_zero(monkeypatch):
    _patch_browser(
        monkeypatch, _dash_result(quality=DashQuality.partial, authority=None),
    )
    adapter = adapter_module.SemrushAdapter()

    result = adapter.collect(_context(), lambda e: None, _NoCancel())

    by_key = {m.metric_key: m for m in result.metrics}
    assert by_key["semrush_authority_score"].value_numeric is None
    assert by_key["semrush_authority_score"].quality == Quality.unavailable
    assert by_key["semrush_organic_traffic"].quality == Quality.partial
    assert result.quality == Quality.partial


def test_collect_truncated_referring_domains_propagates_as_truncated(monkeypatch):
    _patch_browser(monkeypatch, _dash_result(quality=DashQuality.truncated))
    adapter = adapter_module.SemrushAdapter()

    result = adapter.collect(_context(), lambda e: None, _NoCancel())

    assert result.quality == Quality.truncated
    by_key = {m.metric_key: m for m in result.metrics}
    assert by_key["semrush_referring_domains"].quality == Quality.truncated


# ── collect: API fallback only under explicit policy/budget ────────────────

def test_collect_auto_without_budget_never_calls_api_when_browser_unavailable(monkeypatch):
    from seo_audit.dashboard import semrush as real_wrapper

    _patch_browser(monkeypatch, _dash_result(quality=DashQuality.unavailable, authority=None,
                                              traffic=None, keywords=None, refdomains=None))

    def _should_not_be_called(*a, **k):
        raise AssertionError("API fallback must not run without an explicit unit_budget")

    monkeypatch.setattr(real_wrapper, "collect_semrush_api", _should_not_be_called)
    monkeypatch.setenv(adapter_module.SEMRUSH_API_KEY_ENV, "secret-key")
    adapter = adapter_module.SemrushAdapter()

    result = adapter.collect(_context(), lambda e: None, _NoCancel())

    assert result.quality == Quality.unavailable


def test_collect_auto_with_budget_falls_back_to_api_when_browser_unavailable(monkeypatch):
    _patch_browser(monkeypatch, _dash_result(quality=DashQuality.unavailable, authority=None,
                                              traffic=None, keywords=None, refdomains=None))
    _patch_api(
        monkeypatch,
        _dash_result(quality=DashQuality.complete, provenance={"source_mode": "api"}),
    )
    monkeypatch.setenv(adapter_module.SEMRUSH_API_KEY_ENV, "secret-key")
    adapter = adapter_module.SemrushAdapter()
    context = _context(integration_config={"domain": "acme.com", "unit_budget": 200})

    result = adapter.collect(context, lambda e: None, _NoCancel())

    assert result.quality == Quality.complete
    assert result.provenance["source_used"] == "browser+api_fallback"


def test_collect_explicit_api_mode_always_allows_fallback_without_budget(monkeypatch):
    _patch_api(monkeypatch, _dash_result(quality=DashQuality.complete))
    monkeypatch.setenv(adapter_module.SEMRUSH_API_KEY_ENV, "secret-key")
    adapter = adapter_module.SemrushAdapter()
    context = _context(source_mode=SourceMode.api)

    result = adapter.collect(context, lambda e: None, _NoCancel())

    assert result.quality == Quality.complete
    assert result.provenance["source_used"] == "api"


def test_collect_units_exhausted_without_browser_is_unavailable_no_units(monkeypatch):
    _patch_browser(monkeypatch, _dash_result(quality=DashQuality.unavailable, authority=None,
                                              traffic=None, keywords=None, refdomains=None))
    _patch_api(monkeypatch, UnitsExhausted("semrush", "units left: 0"))
    monkeypatch.setenv(adapter_module.SEMRUSH_API_KEY_ENV, "secret-key")
    adapter = adapter_module.SemrushAdapter()
    context = _context(integration_config={"domain": "acme.com", "unit_budget": 200})

    result = adapter.collect(context, lambda e: None, _NoCancel())

    assert result.quality == Quality.unavailable
    assert result.error_code == "no_units"
    assert all(m.value_numeric is None for m in result.metrics)
    assert any("units exhausted" in w.lower() for w in result.warnings)


# ── collect: cancellation / adapter-level guard ─────────────────────────────

def test_collect_cancelled_before_start_raises():
    adapter = adapter_module.SemrushAdapter()
    with pytest.raises(CancelledByAdapter):
        adapter.collect(_context(), lambda e: None, _AlwaysCancel())


def test_collect_unexpected_exception_never_raises(monkeypatch):
    from seo_audit.dashboard import semrush as real_wrapper

    monkeypatch.setattr(adapter_module, "_browser_profile_ready", lambda profile: True)

    def _boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(real_wrapper, "collect_semrush_browser", _boom)
    adapter = adapter_module.SemrushAdapter()

    result = adapter.collect(_context(), lambda e: None, _NoCancel())

    assert result.quality == Quality.unavailable
    assert result.error_code == "adapter_error"
    assert result.metrics == []


def test_collect_no_source_available_is_unavailable(monkeypatch):
    monkeypatch.setattr(adapter_module, "_browser_profile_ready", lambda profile: False)
    monkeypatch.delenv(adapter_module.SEMRUSH_API_KEY_ENV, raising=False)
    adapter = adapter_module.SemrushAdapter()

    result = adapter.collect(_context(source_mode=SourceMode.browser), lambda e: None, _NoCancel())

    assert result.quality == Quality.unavailable
    assert result.error_code == "not_configured"


# ── event bridging / secrets ────────────────────────────────────────────────

def test_bridge_emit_translates_dashboard_event_to_app_event():
    events = []
    on_event = adapter_module._bridge_emit(events.append)

    on_event(
        DashboardProgressEvent(
            provider=DashProvider.semrush, stage="authenticate", message="hi", details={"x": 1}
        )
    )

    assert len(events) == 1
    assert events[0].provider == Provider.semrush
    assert events[0].stage == "authenticate"
    assert events[0].details == {"x": 1}


def test_bridge_emit_never_raises_on_broken_callback():
    def _broken(_event) -> None:
        raise RuntimeError("ui callback exploded")

    on_event = adapter_module._bridge_emit(_broken)
    on_event(DashboardProgressEvent(provider=DashProvider.semrush, stage="authenticate"))


def test_new_artifacts_dir_is_unique_per_call(tmp_path, monkeypatch):
    from app.core import config as config_module

    settings = config_module.Settings(artifacts_dir=tmp_path)
    monkeypatch.setattr(config_module, "get_settings", lambda: settings)

    first = adapter_module._new_artifacts_dir("client-1")
    second = adapter_module._new_artifacts_dir("client-1")

    assert first != second
    assert str(first).startswith(str(tmp_path / "semrush_runs" / "client-1"))


def test_dash_quality_rank_orders_complete_best_and_unavailable_worst():
    rank = adapter_module._dash_quality_rank
    assert rank("complete") < rank("partial")
    assert rank("partial") < rank("unavailable")
