"""Tests for the GSC `ProviderAdapter` (task-09).

`seo_audit.dashboard.gsc.collect_gsc_aggregate`/`collect_gsc_direct` are monkeypatched
throughout — these tests cover only this module's own mapping/combination logic (per-source
quality tracking, canonical key mapping, `gsc_ctr` never emitted here), not the AISeoTools
wrappers themselves (covered by AISeoTools' own `tests/dashboard/test_gsc.py`).
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
    ]
    if both_periods:
        values += [
            _value("clicks", DashPeriodRole.previous, 90, DASH_PREVIOUS),
            _value("impressions", DashPeriodRole.previous, 900, DASH_PREVIOUS),
        ]
    return DashResult(
        provider=DashProvider.gsc, quality=quality, observed_at=datetime.now(UTC),
        data_through=data_through or date(2026, 6, 28),
        current=DASH_CURRENT, previous=DASH_PREVIOUS, values=values,
        provenance={"source_mode": "api", "transport": "reporting_ninja"},
    )


def _direct_result(*, quality=DashQuality.complete, data_through=None) -> DashResult:
    return DashResult(
        provider=DashProvider.gsc, quality=quality, observed_at=datetime.now(UTC),
        data_through=data_through or date(2026, 6, 28),
        current=DASH_CURRENT, previous=DASH_PREVIOUS,
        values=[
            _value(
                "avg_position", DashPeriodRole.current, "4.5", DASH_CURRENT,
                unit=DashUnit.position,
            ),
            _value(
                "avg_position", DashPeriodRole.previous, "5.0", DASH_PREVIOUS,
                unit=DashUnit.position,
            ),
        ],
        provenance={"source_mode": "browser", "transport": "gsc_direct", "profile": "gsc"},
    )


class _NoCancel:
    def __call__(self) -> bool:
        return False


def _patch_sources(monkeypatch, *, aggregate=None, direct=None):
    from seo_audit.dashboard import gsc as real_wrapper

    monkeypatch.setattr(
        real_wrapper, "collect_gsc_aggregate", lambda *a, **k: aggregate or _aggregate_result()
    )
    if direct is not None:
        monkeypatch.setattr(real_wrapper, "collect_gsc_direct", lambda *a, **k: direct)


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


# ── collect: happy path, both sources ───────────────────────────────────────
def test_collect_maps_both_sources_to_canonical_keys(monkeypatch):
    _patch_sources(monkeypatch, direct=_direct_result())
    adapter = adapter_module.GscAdapter()

    result = adapter.collect(_context(), lambda e: None, _NoCancel())

    by_key = {(m.metric_key, m.period_role): m for m in result.metrics}
    assert by_key[("gsc_clicks", PeriodRole.current)].value_numeric == Decimal(100)
    assert by_key[("gsc_impressions", PeriodRole.current)].value_numeric == Decimal(1000)
    assert by_key[("gsc_avg_position", PeriodRole.current)].value_numeric == Decimal("4.5")
    assert result.quality == Quality.complete
    assert len(result.metrics) == 6  # 3 metrics x 2 periods


def test_collect_never_emits_gsc_ctr(monkeypatch):
    _patch_sources(monkeypatch, direct=_direct_result())
    adapter = adapter_module.GscAdapter()

    result = adapter.collect(_context(), lambda e: None, _NoCancel())

    assert "gsc_ctr" not in {m.metric_key for m in result.metrics}


# ── collect: no property configured ─────────────────────────────────────────
def test_collect_without_property_leaves_avg_position_unavailable(monkeypatch):
    _patch_sources(monkeypatch)  # no direct patch — must not even be called
    adapter = adapter_module.GscAdapter()
    context = _context(
        integration_config={"connection_key": "x", "account_id": "sc-domain:example.com"}
    )

    result = adapter.collect(context, lambda e: None, _NoCancel())

    by_key = {(m.metric_key, m.period_role): m for m in result.metrics}
    assert by_key[("gsc_avg_position", PeriodRole.current)].value_numeric is None
    assert by_key[("gsc_avg_position", PeriodRole.current)].quality == Quality.unavailable
    assert by_key[("gsc_clicks", PeriodRole.current)].quality == Quality.complete
    # aggregate complete + avg_position unavailable -> overall partial, never silently complete
    assert result.quality == Quality.partial
    assert any("property" in w for w in result.warnings)


# ── collect: independent source failure ─────────────────────────────────────
def test_collect_direct_failure_does_not_take_down_aggregate(monkeypatch):
    failed_direct = DashResult(
        provider=DashProvider.gsc, quality=DashQuality.unavailable, observed_at=datetime.now(UTC),
        current=DASH_CURRENT, previous=DASH_PREVIOUS, values=[],
        warnings=["gsc session expired"], error_code="auth_required",
        provenance={"source_mode": "browser", "transport": "gsc_direct"},
    )
    _patch_sources(monkeypatch, direct=failed_direct)
    adapter = adapter_module.GscAdapter()

    result = adapter.collect(_context(), lambda e: None, _NoCancel())

    by_key = {(m.metric_key, m.period_role): m for m in result.metrics}
    assert by_key[("gsc_clicks", PeriodRole.current)].quality == Quality.complete
    assert by_key[("gsc_avg_position", PeriodRole.current)].quality == Quality.unavailable
    assert result.quality == Quality.partial
    assert "gsc session expired" in result.warnings


def test_collect_aggregate_failure_does_not_take_down_direct(monkeypatch):
    _patch_sources(
        monkeypatch,
        aggregate=_aggregate_result(quality=DashQuality.unavailable, both_periods=False).model_copy(
            update={"values": []}
        ),
        direct=_direct_result(),
    )
    adapter = adapter_module.GscAdapter()

    result = adapter.collect(_context(), lambda e: None, _NoCancel())

    by_key = {(m.metric_key, m.period_role): m for m in result.metrics}
    assert by_key[("gsc_clicks", PeriodRole.current)].quality == Quality.unavailable
    assert by_key[("gsc_avg_position", PeriodRole.current)].quality == Quality.complete
    assert result.quality == Quality.partial


# ── collect: data_through combination ───────────────────────────────────────
def test_collect_data_through_is_the_earlier_of_the_two_sources(monkeypatch):
    _patch_sources(
        monkeypatch,
        aggregate=_aggregate_result(data_through=date(2026, 6, 28)),
        direct=_direct_result(data_through=date(2026, 6, 26)),
    )
    adapter = adapter_module.GscAdapter()

    result = adapter.collect(_context(), lambda e: None, _NoCancel())

    assert result.data_through == date(2026, 6, 26)


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
