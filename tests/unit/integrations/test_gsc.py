"""Tests for the GSC `ProviderAdapter` (task-09).

`seo_audit.dashboard.gsc.collect_gsc_aggregate` is monkeypatched throughout.
These tests cover the Reporting Ninja-only mapping contract and canonical keys;
the AISeoTools wrapper itself has its own tests.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from seo_audit.dashboard.events import ProgressEvent as DashboardProgressEvent
from seo_audit.dashboard.models import DashboardMetricValue
from seo_audit.dashboard.models import DashboardResult as DashResult
from seo_audit.dashboard.models import DateRange as DashDateRange
from seo_audit.dashboard.models import MetricUnit as DashUnit
from seo_audit.dashboard.models import PeriodRole as DashPeriodRole
from seo_audit.dashboard.models import Provider as DashProvider
from seo_audit.dashboard.models import Quality as DashQuality

from app.integrations import gsc as adapter_module
from app.schemas.adapter import CollectionContext, DateRange
from app.schemas.enums import ComparisonMode, PeriodKind, PeriodRole, Provider, Quality, SourceMode

CURRENT = DateRange(start=date(2026, 6, 1), end=date(2026, 6, 30))
PREVIOUS = DateRange(start=date(2026, 5, 1), end=date(2026, 5, 31))
DASH_CURRENT = DashDateRange(start=CURRENT.start, end=CURRENT.end)
DASH_PREVIOUS = DashDateRange(start=PREVIOUS.start, end=PREVIOUS.end)


def _context(**overrides) -> CollectionContext:
    defaults = dict(
        client_id="client-1",
        provider=Provider.gsc,
        source_mode=SourceMode.auto,
        period_kind=PeriodKind.month,
        comparison_mode=ComparisonMode.closed,
        current=CURRENT,
        previous=PREVIOUS,
        timezone="America/New_York",
        integration_config={
            "connection_key": "seo@example.com",
            "account_id": "sc-domain:example.com",
            "property": "sc-domain:example.com",
        },
    )
    defaults.update(overrides)
    return CollectionContext(**defaults)


def _value(
    key: str, role: DashPeriodRole, value, date_range: DashDateRange, unit=DashUnit.count
) -> DashboardMetricValue:
    return DashboardMetricValue(
        metric_key=key, period_role=role, period_start=date_range.start, period_end=date_range.end,
        value_numeric=Decimal(value) if value is not None else None, unit=unit,
    )


def _aggregate_result(
    *, quality=DashQuality.complete, both_periods=True, data_through=None
) -> DashResult:
    values = [
        _value("clicks", DashPeriodRole.current, 100, DASH_CURRENT),
        _value("impressions", DashPeriodRole.current, 1000, DASH_CURRENT),
        _value(
            "avg_position", DashPeriodRole.current, "4.5", DASH_CURRENT,
            unit=DashUnit.position,
        ),
    ]
    if both_periods:
        values += [
            _value("clicks", DashPeriodRole.previous, 90, DASH_PREVIOUS),
            _value("impressions", DashPeriodRole.previous, 900, DASH_PREVIOUS),
            _value(
                "avg_position", DashPeriodRole.previous, "5.0", DASH_PREVIOUS,
                unit=DashUnit.position,
            ),
        ]
    return DashResult(
        provider=DashProvider.gsc, quality=quality, observed_at=datetime.now(UTC),
        data_through=data_through or date(2026, 6, 28),
        current=DASH_CURRENT, previous=DASH_PREVIOUS, values=values,
        provenance={"source_mode": "api", "transport": "reporting_ninja"},
    )


class _NoCancel:
    def __call__(self) -> bool:
        return False


def _patch_sources(monkeypatch, *, aggregate=None):
    from seo_audit.dashboard import gsc as real_wrapper

    monkeypatch.setattr(
        real_wrapper, "collect_gsc_aggregate", lambda *a, **k: aggregate or _aggregate_result()
    )


# ── preflight ────────────────────────────────────────────────────────────────
def test_preflight_missing_config_is_not_configured():
    adapter = adapter_module.GscAdapter()
    result = adapter.preflight(_context(integration_config={}))
    assert result.ok is False
    assert result.reason_code == "not_configured"


def test_preflight_property_not_required():
    """`property` (needed only for avg_position) is optional — aggregate clicks/impressions
    stand on their own per PLAN §8.3, so preflight must not require it."""
    adapter = adapter_module.GscAdapter()
    cfg = {"connection_key": "x", "account_id": "sc-domain:example.com"}
    result = adapter.preflight(_context(integration_config=cfg))
    # Only blocked by the API key below, not by `property` being absent.
    assert result.reason_code != "not_configured" or "property" not in (result.message or "")


def test_preflight_no_api_key_is_auth_required(monkeypatch):
    monkeypatch.delenv("REPORTING_NINJA_API_KEY", raising=False)
    adapter = adapter_module.GscAdapter()
    result = adapter.preflight(_context())
    assert result.ok is False
    assert result.reason_code == "auth_required"


def test_preflight_with_api_key_is_ok(monkeypatch):
    monkeypatch.setenv("REPORTING_NINJA_API_KEY", "secret-key")
    adapter = adapter_module.GscAdapter()
    result = adapter.preflight(_context())
    assert result.ok is True


# ── collect: Reporting Ninja happy path ─────────────────────────────────────
def test_collect_maps_api_metrics_to_canonical_keys(monkeypatch):
    _patch_sources(monkeypatch)
    adapter = adapter_module.GscAdapter()

    result = adapter.collect(_context(), lambda e: None, _NoCancel())

    by_key = {(m.metric_key, m.period_role): m for m in result.metrics}
    assert by_key[("gsc_clicks", PeriodRole.current)].value_numeric == Decimal(100)
    assert by_key[("gsc_impressions", PeriodRole.current)].value_numeric == Decimal(1000)
    assert by_key[("gsc_avg_position", PeriodRole.current)].value_numeric == Decimal("4.5")
    assert result.quality == Quality.complete
    assert len(result.metrics) == 6  # 3 metrics x 2 periods


def test_collect_reuses_discovered_fields_across_clients(monkeypatch):
    from seo_audit.clientreport import api_client
    from seo_audit.dashboard import gsc as real_wrapper

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

    seen_field_ids = []
    seen_bypass_cache = []

    def fake_aggregate(*args, field_ids=None, **kwargs):
        seen_field_ids.append(field_ids)
        seen_bypass_cache.append(kwargs.get("bypass_cache"))
        return _aggregate_result().model_copy(
            update={"provenance": {"gsc_fields": dict(fields)}}
        )

    fields = {
        "clicks": "clicks",
        "impressions": "impressions",
        "ctr": "ctr",
        "avg_position": "avgposition",
    }
    monkeypatch.setattr(api_client, "ReportingNinjaClient", FakeClient)
    monkeypatch.setattr(real_wrapper, "collect_gsc_aggregate", fake_aggregate)
    adapter = adapter_module.GscAdapter()

    adapter.collect(_context(force_refresh=True), lambda event: None, _NoCancel())
    adapter.collect(_context(force_refresh=True), lambda event: None, _NoCancel())

    assert seen_field_ids == [None, fields]
    assert seen_bypass_cache == [True, True]


def test_collect_never_emits_gsc_ctr(monkeypatch):
    _patch_sources(monkeypatch)
    adapter = adapter_module.GscAdapter()

    result = adapter.collect(_context(), lambda e: None, _NoCancel())

    assert "gsc_ctr" not in {m.metric_key for m in result.metrics}


# ── collect: API mode does not need the legacy browser property ─────────────
def test_collect_without_property_keeps_api_position_available(monkeypatch):
    _patch_sources(monkeypatch)  # no direct patch — must not even be called
    adapter = adapter_module.GscAdapter()
    context = _context(
        integration_config={"connection_key": "x", "account_id": "sc-domain:example.com"}
    )

    result = adapter.collect(context, lambda e: None, _NoCancel())

    by_key = {(m.metric_key, m.period_role): m for m in result.metrics}
    assert by_key[("gsc_avg_position", PeriodRole.current)].value_numeric == Decimal("4.5")
    assert by_key[("gsc_avg_position", PeriodRole.current)].quality == Quality.complete
    assert by_key[("gsc_clicks", PeriodRole.current)].quality == Quality.complete
    assert result.quality == Quality.complete
    assert result.warnings == []


def test_legacy_browser_mode_still_uses_reporting_ninja_only(monkeypatch):
    from seo_audit.dashboard import gsc as real_wrapper

    _patch_sources(monkeypatch, aggregate=_aggregate_result(data_through=date(2026, 6, 28)))

    def browser_must_not_run(*args, **kwargs):
        raise AssertionError("browser GSC must not run from the dashboard adapter")

    monkeypatch.setattr(real_wrapper, "collect_gsc_direct", browser_must_not_run)
    adapter = adapter_module.GscAdapter()

    result = adapter.collect(
        _context(source_mode=SourceMode.browser), lambda e: None, _NoCancel()
    )

    assert result.quality == Quality.complete
    assert result.data_through == date(2026, 6, 28)
    assert result.provenance["aggregate"]["transport"] == "reporting_ninja"
    assert "direct" not in result.provenance


# ── collect: adapter-level guard ────────────────────────────────────────────
def test_collect_unexpected_exception_never_raises(monkeypatch):
    from seo_audit.dashboard import gsc as real_wrapper

    def _boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(real_wrapper, "collect_gsc_aggregate", _boom)
    adapter = adapter_module.GscAdapter()

    result = adapter.collect(_context(), lambda e: None, _NoCancel())

    assert result.quality == Quality.unavailable
    assert result.error_code == "adapter_error"
    assert result.metrics == []


def test_collect_datasets_fetches_real_daily_gsc_rows(monkeypatch):
    from seo_audit.clientreport import api_client, gsc_fetcher

    class FakeClient:
        bypass_cache_values = []

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def query_all(self, *args, **kwargs):
            self.bypass_cache_values.append(kwargs.get("bypass_cache"))
            start = args[4]
            return {
                "rows": [
                    {
                        "date": start,
                        "clicks": 12,
                        "impressions": 120,
                        "ctr": 10,
                        "avgposition": 4.5,
                    },
                    {
                        "date": str(date.fromisoformat(start).replace(day=2)),
                        "clicks": 15,
                        "impressions": 150,
                        "ctr": 10,
                        "avgposition": 4.0,
                    },
                ],
                "truncated": False,
            }

    monkeypatch.setattr(api_client, "ReportingNinjaClient", FakeClient)
    monkeypatch.setattr(
        gsc_fetcher,
        "discover_gsc_fields",
        lambda client: {
            "clicks": "clicks",
            "impressions": "impressions",
            "ctr": "ctr",
            "avg_position": "avgposition",
        },
    )

    events = []
    result = adapter_module.GscAdapter().collect_datasets(
        _context(force_refresh=True), events.append
    )

    assert result.quality == Quality.complete
    assert len(result.rows) == 4
    assert result.rows[0].dataset_key == "gsc_daily"
    assert result.rows[0].dimension_value == "2026-06-01"
    assert result.rows[0].metrics["clicks"] == Decimal(12)
    assert result.rows[0].metrics["ctr"] == Decimal("0.1")
    assert result.rows[2].period_role == PeriodRole.previous
    assert FakeClient.bypass_cache_values == [True, True]
    # task-32: кроки вимірюються В МЕЖАХ фази датасетів, а не за наскрізною
    # шкалою «7..9 з 9», яка доводила клітинку до 100 % ще до persist.
    # Послідовність: межа фази → два періоди → закриття фази.
    assert [event.stage for event in events] == [
        "datasets",
        "fetch_current_daily",
        "fetch_previous_daily",
        "datasets_complete",
    ]
    assert [event.completed for event in events] == [None, 1, 2, None]
    # GSC: kpi 30 + dimensions 40 → фаза датасетів займає 70..95 %.
    percents = [event.percent for event in events]
    assert percents[0] == pytest.approx(70.0)
    assert percents[-1] == pytest.approx(95.0)
    assert percents == sorted(percents)


# ── event bridging ───────────────────────────────────────────────────────────
def test_bridge_emit_translates_dashboard_event_to_app_event():
    events = []
    on_event = adapter_module._bridge_emit(events.append)

    on_event(
        DashboardProgressEvent(
            provider=DashProvider.gsc, stage="fetch_current", message="hi", details={}
        )
    )

    assert len(events) == 1
    assert events[0].provider == Provider.gsc
    assert events[0].stage == "fetch_current"


def test_bridge_emit_never_raises_on_broken_callback():
    def _broken(_event) -> None:
        raise RuntimeError("ui callback exploded")

    on_event = adapter_module._bridge_emit(_broken)
    on_event(DashboardProgressEvent(provider=DashProvider.gsc, stage="discover"))
