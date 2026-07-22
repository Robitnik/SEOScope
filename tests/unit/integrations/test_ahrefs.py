"""Tests for the Ahrefs `ProviderAdapter` (task-10).

`seo_audit.dashboard.ahrefs.collect_site_audit` is monkeypatched throughout —
these tests cover only this module's own mapping/business-rule logic
(quality, staleness, fallback, units-exhausted handling), not the AISeoTools
wrapper itself (covered by AISeoTools' own `tests/dashboard/test_ahrefs.py`).
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

from app.integrations import ahrefs as adapter_module
from app.jobs.fake_adapter import CancelledByAdapter
from app.schemas.adapter import CollectionContext, DateRange
from app.schemas.enums import ComparisonMode, PeriodKind, Provider, Quality, SourceMode


def _context(**overrides) -> CollectionContext:
    defaults = dict(
        client_id="client-1",
        provider=Provider.ahrefs,
        source_mode=SourceMode.api,
        period_kind=PeriodKind.week,
        comparison_mode=ComparisonMode.closed,
        current=DateRange(start=date(2026, 7, 13), end=date(2026, 7, 19)),
        previous=DateRange(start=date(2026, 7, 6), end=date(2026, 7, 12)),
        timezone="America/New_York",
        integration_config={"domain": "acme.com"},
    )
    defaults.update(overrides)
    return CollectionContext(**defaults)


def _dash_value(key: str, value: int | None, unit=DashUnit.count) -> DashboardMetricValue:
    return DashboardMetricValue(
        metric_key=key, period_role=DashPeriodRole.snapshot,
        value_numeric=Decimal(value) if value is not None else None, unit=unit,
    )


def _dash_result(
    *, quality=DashQuality.complete, data_through: date | None = date(2026, 7, 15),
    health=80, errors=1, warnings_count=2, urls_crawled=100, warnings=None,
) -> DashResult:
    return DashResult(
        provider=DashProvider.ahrefs,
        quality=quality,
        observed_at=datetime.now(UTC),
        data_through=data_through,
        values=[
            _dash_value("ahrefs_health_score", health, unit=DashUnit.score),
            _dash_value("ahrefs_errors", errors),
            _dash_value("ahrefs_warnings", warnings_count),
            _dash_value("ahrefs_urls_crawled", urls_crawled),
        ],
        warnings=warnings or [],
        provenance={"source_mode": "api", "project_id": "p1"},
    )


class _NoCancel:
    def __call__(self) -> bool:
        return False


class _AlwaysCancel:
    def __call__(self) -> bool:
        return True


# ── preflight ────────────────────────────────────────────────────────────────

def test_preflight_missing_domain_is_not_configured():
    adapter = adapter_module.AhrefsAdapter()
    context = _context(integration_config={})

    result = adapter.preflight(context)

    assert result.ok is False
    assert result.reason_code == "not_configured"


def test_preflight_no_api_key_no_csv_is_auth_required(monkeypatch):
    monkeypatch.delenv(adapter_module.AHREFS_API_KEY_ENV, raising=False)
    adapter = adapter_module.AhrefsAdapter()

    result = adapter.preflight(_context(source_mode=SourceMode.api))

    assert result.ok is False
    assert result.reason_code == "auth_required"


def test_preflight_with_api_key_is_ok(monkeypatch):
    monkeypatch.setenv(adapter_module.AHREFS_API_KEY_ENV, "secret-key")
    adapter = adapter_module.AhrefsAdapter()

    result = adapter.preflight(_context(source_mode=SourceMode.api))

    assert result.ok is True
    assert result.units_estimated == adapter_module._UNITS_ESTIMATE


def test_preflight_pdf_source_mode_unsupported(monkeypatch):
    monkeypatch.setenv(adapter_module.AHREFS_API_KEY_ENV, "secret-key")
    adapter = adapter_module.AhrefsAdapter()

    result = adapter.preflight(_context(source_mode=SourceMode.pdf))

    assert result.ok is False
    assert result.reason_code == "not_configured"


def test_preflight_browser_without_csv_dir_is_not_configured(monkeypatch):
    monkeypatch.delenv(adapter_module.AHREFS_API_KEY_ENV, raising=False)
    adapter = adapter_module.AhrefsAdapter()

    result = adapter.preflight(_context(source_mode=SourceMode.browser))

    assert result.ok is False
    assert result.reason_code == "not_configured"


def test_preflight_browser_with_existing_csv_dir_is_ok(tmp_path, monkeypatch):
    monkeypatch.delenv(adapter_module.AHREFS_API_KEY_ENV, raising=False)
    adapter = adapter_module.AhrefsAdapter()
    context = _context(
        source_mode=SourceMode.browser,
        integration_config={"domain": "acme.com", "csv_dir": str(tmp_path)},
    )

    result = adapter.preflight(context)

    assert result.ok is True
    assert result.units_estimated == 0


def test_preflight_auto_without_key_falls_back_to_csv_availability(tmp_path, monkeypatch):
    monkeypatch.delenv(adapter_module.AHREFS_API_KEY_ENV, raising=False)
    adapter = adapter_module.AhrefsAdapter()
    context = _context(
        source_mode=SourceMode.auto,
        integration_config={"domain": "acme.com", "csv_dir": str(tmp_path)},
    )

    result = adapter.preflight(context)

    assert result.ok is True


# ── collect: happy path ──────────────────────────────────────────────────────

def test_collect_maps_complete_result(monkeypatch):
    monkeypatch.setenv(adapter_module.AHREFS_API_KEY_ENV, "secret-key")
    monkeypatch.setattr(adapter_module, "collect_site_audit", lambda *a, **k: _dash_result())
    adapter = adapter_module.AhrefsAdapter()

    result = adapter.collect(_context(), lambda e: None, _NoCancel())

    assert result.quality == Quality.complete
    assert len(result.metrics) == 4
    assert all(m.quality == Quality.complete for m in result.metrics)
    assert result.data_through == date(2026, 7, 15)
    assert result.provenance["fallback_used"] is False


def test_bridge_emit_translates_dashboard_event_to_app_event():
    events = []
    on_event = adapter_module._bridge_emit(events.append)

    on_event(
        DashboardProgressEvent(
            provider=DashProvider.ahrefs, stage="site_audit", message="hello", details={"x": 1}
        )
    )

    assert len(events) == 1
    assert events[0].provider == Provider.ahrefs
    assert events[0].stage == "site_audit"
    assert events[0].message == "hello"
    assert events[0].details == {"x": 1}


def test_collect_missing_value_is_unavailable_not_zero(monkeypatch):
    monkeypatch.setenv(adapter_module.AHREFS_API_KEY_ENV, "secret-key")
    monkeypatch.setattr(
        adapter_module, "collect_site_audit",
        lambda *a, **k: _dash_result(quality=DashQuality.partial, health=None),
    )
    adapter = adapter_module.AhrefsAdapter()

    result = adapter.collect(_context(), lambda e: None, _NoCancel())

    by_key = {m.metric_key: m for m in result.metrics}
    assert by_key["ahrefs_health_score"].value_numeric is None
    assert by_key["ahrefs_health_score"].quality == Quality.unavailable
    assert by_key["ahrefs_errors"].quality == Quality.complete
    # One unavailable metric drags the aggregate down.
    assert result.quality == Quality.unavailable


def test_collect_stale_crawl_marks_metrics_stale_and_warns(monkeypatch):
    monkeypatch.setenv(adapter_module.AHREFS_API_KEY_ENV, "secret-key")
    monkeypatch.setattr(
        adapter_module, "collect_site_audit",
        lambda *a, **k: _dash_result(data_through=date(2026, 6, 1)),
    )
    adapter = adapter_module.AhrefsAdapter()

    result = adapter.collect(_context(), lambda e: None, _NoCancel())

    assert all(m.quality == Quality.stale for m in result.metrics)
    assert result.quality == Quality.stale
    assert any("stale" in w.lower() for w in result.warnings)


# ── collect: fallback / units exhausted ─────────────────────────────────────

def test_collect_auto_falls_back_to_csv_when_api_finds_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv(adapter_module.AHREFS_API_KEY_ENV, "secret-key")

    calls = []

    def _fake(domain, *, csv_dir=None, **kwargs):
        calls.append(csv_dir)
        if csv_dir is None:
            return _dash_result(quality=DashQuality.unavailable, health=None, errors=None,
                                 warnings_count=None, urls_crawled=None, data_through=None)
        return _dash_result(quality=DashQuality.partial, health=None)

    monkeypatch.setattr(adapter_module, "collect_site_audit", _fake)
    adapter = adapter_module.AhrefsAdapter()
    context = _context(
        source_mode=SourceMode.auto,
        integration_config={"domain": "acme.com", "csv_dir": str(tmp_path)},
    )

    result = adapter.collect(context, lambda e: None, _NoCancel())

    assert calls == [None, str(tmp_path)]
    assert result.provenance["fallback_used"] is True
    by_key = {m.metric_key: m for m in result.metrics}
    # The CSV fallback's issue-type counts made it through even though its
    # health score (never available off a CSV export) is still `unavailable`.
    assert by_key["ahrefs_errors"].quality == Quality.complete
    assert by_key["ahrefs_health_score"].quality == Quality.unavailable


def test_collect_units_exhausted_without_csv_is_unavailable_no_units(monkeypatch):
    monkeypatch.setenv(adapter_module.AHREFS_API_KEY_ENV, "secret-key")

    def _raise(*a, **k):
        raise UnitsExhausted("ahrefs", "units left: 0")

    monkeypatch.setattr(adapter_module, "collect_site_audit", _raise)
    adapter = adapter_module.AhrefsAdapter()

    result = adapter.collect(_context(), lambda e: None, _NoCancel())

    assert result.quality == Quality.unavailable
    assert result.error_code == "no_units"
    assert result.metrics == []


def test_collect_units_exhausted_falls_back_to_csv(tmp_path, monkeypatch):
    monkeypatch.setenv(adapter_module.AHREFS_API_KEY_ENV, "secret-key")

    def _fake(domain, *, csv_dir=None, **kwargs):
        if csv_dir is None:
            raise UnitsExhausted("ahrefs", "units left: 0")
        return _dash_result(quality=DashQuality.complete)

    monkeypatch.setattr(adapter_module, "collect_site_audit", _fake)
    adapter = adapter_module.AhrefsAdapter()
    context = _context(
        source_mode=SourceMode.auto,
        integration_config={"domain": "acme.com", "csv_dir": str(tmp_path)},
    )

    result = adapter.collect(context, lambda e: None, _NoCancel())

    assert result.quality == Quality.complete
    assert result.provenance["fallback_used"] is True


def test_collect_cancelled_before_start_raises(monkeypatch):
    monkeypatch.setenv(adapter_module.AHREFS_API_KEY_ENV, "secret-key")
    adapter = adapter_module.AhrefsAdapter()

    with pytest.raises(CancelledByAdapter):
        adapter.collect(_context(), lambda e: None, _AlwaysCancel())


def test_collect_no_source_available_is_unavailable(monkeypatch):
    monkeypatch.delenv(adapter_module.AHREFS_API_KEY_ENV, raising=False)
    adapter = adapter_module.AhrefsAdapter()

    result = adapter.collect(_context(source_mode=SourceMode.api), lambda e: None, _NoCancel())

    assert result.quality == Quality.unavailable
    assert result.error_code == "collection_unavailable"
