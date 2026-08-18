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


def test_collect_datasets_fetches_daily_and_breakdowns(monkeypatch):
    from seo_audit.clientreport import api_client

    class FakeClient:
        calls = []

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def list_fields(self, integration_id):
            assert integration_id == "ga4"
            fields = {
                definition["dimension"]
                for definition in adapter_module._GA4_DATASETS.values()
            }
            fields.update(
                field
                for definition in adapter_module._GA4_DATASETS.values()
                for field in definition["metrics"].values()
            )
            fields.add(adapter_module._CHANNEL_DIMENSION)
            return [{"field_id": field} for field in fields]

        def query_all(
            self, integration_id, connection_key, account_id, fields, start, end, **kwargs
        ):
            self.calls.append((fields, start, end))
            dimension = fields[0]
            dimension_value = "20260615" if dimension == "date" else f"value-{dimension}"
            return {
                "rows": [
                    {
                        dimension: dimension_value,
                        **{field: "10.5" for field in fields[1:]},
                    }
                ],
                "truncated": False,
            }

    monkeypatch.setattr(api_client, "ReportingNinjaClient", FakeClient)

    result = adapter_module.ReportingNinjaAdapter().collect_datasets(
        _context(), lambda event: None
    )

    assert result.quality == Quality.complete
    # +2 (task-44): one `ga4_daily_channels` row per role, on top of the
    # single-dimension datasets in `_GA4_DATASETS`.
    assert len(result.rows) == len(adapter_module._GA4_DATASETS) * 2 + 2
    daily = next(row for row in result.rows if row.dataset_key == "ga4_daily")
    assert daily.dimension_value == "2026-06-15"
    assert daily.metrics["users"] == Decimal("10.5")
    channels = [row for row in result.rows if row.dataset_key == "ga4_daily_channels"]
    assert len(channels) == 2
    assert channels[0].dimension_value == "2026-06-15"
    # This fake never returns a real "Organic Search"/"Direct" label, so
    # neither field matches — both stay `None`, never a fabricated `0`.
    assert channels[0].metrics == {"conversions_organic": None, "conversions_direct": None}
    assert len(FakeClient.calls) == 14


def test_collect_datasets_aggregates_business_profile_locations(monkeypatch):
    from seo_audit.clientreport import api_client

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def list_fields(self, integration_id):
            fields = {
                definition["dimension"]
                for definition in adapter_module._GA4_DATASETS.values()
            }
            fields.update(
                field
                for definition in adapter_module._GA4_DATASETS.values()
                for field in definition["metrics"].values()
            )
            fields.add(adapter_module._CHANNEL_DIMENSION)
            return [{"field_id": field} for field in fields]

        def query_all(
            self, integration_id, connection_key, account_id, fields, start, end, **kwargs
        ):
            if integration_id == "ga4":
                return {
                    "rows": [
                        {
                            fields[0]: "20260615" if fields[0] == "date" else "GA4 value",
                            **{field: "10" for field in fields[1:]},
                        }
                    ]
                }
            if kwargs["data_view"] == "performance":
                return {
                    "rows": [
                        {
                            "day": "2026-06-15",
                            **{
                                field: "2"
                                for field in adapter_module._GBP_PERFORMANCE_METRICS.values()
                            },
                        }
                    ]
                }
            return {
                "rows": [{"search_terms": "sapphire nyc", "search_impressions": "9"}]
            }

        def query_page(self, *args, **kwargs):
            return {
                "rows": [{"reviews_lifetime": "100", "average_rating_lifetime": "4.7"}]
            }

    monkeypatch.setattr(api_client, "ReportingNinjaClient", FakeClient)
    context = _context(
        integration_config={
            "connection_key": "seo@example.com",
            "account_id": "properties/1",
            "gbp_locations": [
                {
                    "connection_key": "seo@example.com",
                    "account_id": "accounts/1/locations/1",
                    "name": "Sapphire Times Square",
                }
            ],
        }
    )

    result = adapter_module.ReportingNinjaAdapter().collect_datasets(
        context, lambda event: None
    )

    assert result.quality == Quality.complete
    location = next(row for row in result.rows if row.dataset_key == "gbp_locations")
    term = next(row for row in result.rows if row.dataset_key == "gbp_search_terms")
    review = next(row for row in result.rows if row.dataset_key == "gbp_reviews")
    assert location.dimension_value == "Sapphire Times Square"
    assert location.metrics["interactions"] == Decimal(2)
    assert term.metrics["impressions"] == Decimal(9)
    assert review.metrics["average_rating"] == Decimal("4.7")


# ── ga4_daily_channels (task-44) ─────────────────────────────────────────────
def test_query_daily_channels_none_for_absent_channel_real_zero_stays_zero():
    """The core PLAN §12 rule for this dataset: a channel missing from a
    day's response is `None`; a channel present with `keyEvents=0` is a real,
    preserved `0`; a day entirely absent from the response gets no row."""

    class FakeClient:
        def query_all(self, *args, **kwargs):
            return {
                "rows": [
                    {"date": "20260701", "firstUserDefaultChannelGroup": "Organic Search",
                     "keyEvents": "12"},
                    # Direct is absent on 07-01 -> None, not 0.
                    {"date": "20260702", "firstUserDefaultChannelGroup": "Organic Search",
                     "keyEvents": "5"},
                    {"date": "20260702", "firstUserDefaultChannelGroup": "Direct",
                     "keyEvents": "0"},
                    # Paid Search is not one of the two tracked channels -> ignored.
                    {"date": "20260702", "firstUserDefaultChannelGroup": "Paid Search",
                     "keyEvents": "99"},
                ],
                "truncated": False,
            }

    rows, truncated = adapter_module.query_daily_channels(
        FakeClient(),
        connection_key="seo@example.com",
        account_id="properties/1",
        start=date(2026, 7, 1),
        end=date(2026, 7, 2),
    )

    assert truncated is False
    by_day = dict(rows)
    assert by_day["2026-07-01"] == {
        "conversions_organic": Decimal(12),
        "conversions_direct": None,
    }
    assert by_day["2026-07-02"] == {
        "conversions_organic": Decimal(5),
        "conversions_direct": Decimal(0),
    }
    # A day never returned by the API (e.g. 07-03) simply never appears.
    assert "2026-07-03" not in by_day


def test_query_daily_channels_organic_plus_direct_never_exceeds_total():
    """Accepted-gate invariant (task-44 plan §6):
    conversions_organic + conversions_direct <= conversions (keyEvents total)
    for every day, since Organic Search and Direct are two of several channels."""

    class FakeClient:
        def query_all(self, *args, **kwargs):
            return {
                "rows": [
                    {"date": "20260701", "firstUserDefaultChannelGroup": "Organic Search",
                     "keyEvents": "6"},
                    {"date": "20260701", "firstUserDefaultChannelGroup": "Direct",
                     "keyEvents": "3"},
                    {"date": "20260701", "firstUserDefaultChannelGroup": "Referral",
                     "keyEvents": "4"},
                ],
                "truncated": False,
            }

    rows, _truncated = adapter_module.query_daily_channels(
        FakeClient(),
        connection_key="seo@example.com",
        account_id="properties/1",
        start=date(2026, 7, 1),
        end=date(2026, 7, 1),
    )

    day, metrics = rows[0]
    total_keyevents = Decimal(6) + Decimal(3) + Decimal(4)  # organic + direct + referral
    assert metrics["conversions_organic"] + metrics["conversions_direct"] <= total_keyevents


def test_collect_datasets_ga4_daily_channels_failure_does_not_fail_the_rest(monkeypatch):
    from seo_audit.clientreport import api_client

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def list_fields(self, integration_id):
            fields = {
                definition["dimension"]
                for definition in adapter_module._GA4_DATASETS.values()
            }
            fields.update(
                field
                for definition in adapter_module._GA4_DATASETS.values()
                for field in definition["metrics"].values()
            )
            fields.add(adapter_module._CHANNEL_DIMENSION)
            return [{"field_id": field} for field in fields]

        def query_all(
            self, integration_id, connection_key, account_id, fields, start, end, **kwargs
        ):
            if adapter_module._CHANNEL_DIMENSION in fields:
                raise api_client.APIError("adapter_error", "ga4 channel query failed")
            dimension = fields[0]
            dimension_value = "20260615" if dimension == "date" else f"value-{dimension}"
            return {
                "rows": [
                    {dimension: dimension_value, **{field: "10.5" for field in fields[1:]}}
                ],
                "truncated": False,
            }

    monkeypatch.setattr(api_client, "ReportingNinjaClient", FakeClient)

    result = adapter_module.ReportingNinjaAdapter().collect_datasets(
        _context(), lambda event: None
    )

    assert not any(row.dataset_key == "ga4_daily_channels" for row in result.rows)
    assert any("ga4_daily_channels" in warning for warning in result.warnings)
    # The other six single-dimension datasets still succeeded for both periods.
    assert len(result.rows) == len(adapter_module._GA4_DATASETS) * 2


def test_collect_datasets_dataset_steps_account_for_ga4_daily_channels(monkeypatch):
    from seo_audit.clientreport import api_client

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def list_fields(self, integration_id):
            fields = {
                definition["dimension"]
                for definition in adapter_module._GA4_DATASETS.values()
            }
            fields.update(
                field
                for definition in adapter_module._GA4_DATASETS.values()
                for field in definition["metrics"].values()
            )
            fields.add(adapter_module._CHANNEL_DIMENSION)
            return [{"field_id": field} for field in fields]

        def query_all(
            self, integration_id, connection_key, account_id, fields, start, end, **kwargs
        ):
            dimension = fields[0]
            dimension_value = "20260615" if dimension == "date" else f"value-{dimension}"
            return {
                "rows": [
                    {dimension: dimension_value, **{field: "10.5" for field in fields[1:]}}
                ],
                "truncated": False,
            }

    monkeypatch.setattr(api_client, "ReportingNinjaClient", FakeClient)
    totals: list[int] = []
    original_step = adapter_module.PhaseTracker.step

    def _capturing_step(self, stage, *, completed, total, **kwargs):
        totals.append(total)
        return original_step(self, stage, completed=completed, total=total, **kwargs)

    monkeypatch.setattr(adapter_module.PhaseTracker, "step", _capturing_step)

    adapter_module.ReportingNinjaAdapter().collect_datasets(_context(), lambda event: None)

    # (6 datasets + 1 new ga4_daily_channels) * 2 periods = 14 (plan §2.4).
    assert totals and all(total == (len(adapter_module._GA4_DATASETS) + 1) * 2 for total in totals)


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
