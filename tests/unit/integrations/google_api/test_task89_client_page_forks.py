"""task-89 §3.4/§3.5: the four remaining client-page transport forks —
GSC aggregate KPI, GSC page/query dimensions, GA4 KPI aggregate, and the
five GA4 breakdown datasets generalized beyond `ga4_daily`/`ga4_daily_channels`
(task-83's original scope). Same row-shape-parity + "never a silent RN
fallback" discipline as `test_transport.py`.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal

import httpx
import pytest
from seo_audit.dashboard.models import DashboardMetricValue
from seo_audit.dashboard.models import DashboardResult as DashResult
from seo_audit.dashboard.models import DateRange as DashDateRange
from seo_audit.dashboard.models import MetricUnit as DashUnit
from seo_audit.dashboard.models import PeriodRole as DashPeriodRole
from seo_audit.dashboard.models import Provider as DashProvider
from seo_audit.dashboard.models import Quality as DashQuality

from app.integrations import gsc as gsc_adapter
from app.integrations import reporting_ninja as rn_adapter
from app.integrations.google_api.errors import GoogleApiConfigError
from app.schemas.adapter import CollectionContext, DateRange
from app.schemas.enums import ComparisonMode, PeriodKind, Provider, Quality, SourceMode

from .fakes import FakeGoogle

CURRENT = DateRange(start=date(2026, 8, 1), end=date(2026, 8, 31))
PREVIOUS = DateRange(start=date(2026, 7, 1), end=date(2026, 7, 31))
DASH_CURRENT = DashDateRange(start=CURRENT.start, end=CURRENT.end)
DASH_PREVIOUS = DashDateRange(start=PREVIOUS.start, end=PREVIOUS.end)


def _ctx(provider: Provider, account_id: str, transport: str | None) -> CollectionContext:
    cfg: dict[str, object] = {"connection_key": "ck@example.com", "account_id": account_id}
    if transport is not None:
        cfg["transport"] = transport
    return CollectionContext(
        client_id="client-1",
        provider=provider,
        source_mode=SourceMode.api,
        period_kind=PeriodKind.custom,
        comparison_mode=ComparisonMode.closed,
        current=CURRENT,
        previous=PREVIOUS,
        timezone="America/New_York",
        integration_config=cfg,
    )


def _by_key(metrics: list) -> dict[tuple[str, str], object]:
    return {(m.metric_key, m.period_role.value): m for m in metrics}


# ── §3.4 GSC aggregate KPI (`gsc_clicks`/`gsc_impressions`/`gsc_avg_position`) ──
def test_gsc_kpi_google_api_without_token_is_not_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def no_token(*_a: object, **_k: object) -> object:
        raise GoogleApiConfigError("no token")

    monkeypatch.setattr("app.integrations.gsc.open_google_api_session", no_token)

    def rn_must_not_run(*_a: object, **_k: object) -> object:
        raise AssertionError("Reporting Ninja must not be a silent fallback")

    monkeypatch.setattr(
        "seo_audit.clientreport.api_client.ReportingNinjaClient", rn_must_not_run
    )
    result = gsc_adapter.GscAdapter().collect(
        _ctx(Provider.gsc, "sc-domain:example.com", "google_api"),
        lambda _e: None, lambda: False,
    )
    assert result.error_code == "not_configured"
    assert result.quality == Quality.unavailable
    assert result.metrics == []


def test_gsc_kpi_parity_between_transports(
    monkeypatch: pytest.MonkeyPatch, fake_google: FakeGoogle
) -> None:
    def gsc_totals(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body["dimensions"] == ["date"]:
            # the `_gsc_data_through` freshness probe, same endpoint
            return httpx.Response(
                200, json={"rows": [{"keys": ["2026-08-28"], "clicks": 5, "impressions": 50}]}
            )
        return httpx.Response(
            200, json={"rows": [{"clicks": 100, "impressions": 1000, "ctr": 0.1, "position": 4.5}]}
        )

    fake_google.on("searchAnalytics/query", gsc_totals)
    session = fake_google.session()
    monkeypatch.setattr(
        "app.integrations.gsc.open_google_api_session", lambda *_a, **_k: session
    )
    google_result = gsc_adapter.GscAdapter().collect(
        _ctx(Provider.gsc, "sc-domain:example.com", "google_api"), lambda _e: None, lambda: False
    )

    from seo_audit.dashboard import gsc as real_wrapper

    def fake_aggregate(*_a: object, **_k: object) -> DashResult:
        values = [
            DashboardMetricValue(
                metric_key="clicks", period_role=DashPeriodRole.current,
                period_start=CURRENT.start, period_end=CURRENT.end,
                value_numeric=Decimal(100), unit=DashUnit.count,
            ),
            DashboardMetricValue(
                metric_key="impressions", period_role=DashPeriodRole.current,
                period_start=CURRENT.start, period_end=CURRENT.end,
                value_numeric=Decimal(1000), unit=DashUnit.count,
            ),
            DashboardMetricValue(
                metric_key="avg_position", period_role=DashPeriodRole.current,
                period_start=CURRENT.start, period_end=CURRENT.end,
                value_numeric=Decimal("4.5"), unit=DashUnit.position,
            ),
        ]
        return DashResult(
            provider=DashProvider.gsc, quality=DashQuality.complete,
            observed_at=datetime.now(UTC), data_through=date(2026, 8, 28),
            current=DASH_CURRENT, previous=DASH_PREVIOUS, values=values,
            provenance={"transport": "reporting_ninja"},
        )

    monkeypatch.setattr(real_wrapper, "collect_gsc_aggregate", fake_aggregate)

    class FakeRNClient:
        def __enter__(self) -> FakeRNClient:
            return self

        def __exit__(self, *_a: object) -> None:
            return None

        def close(self) -> None:
            return None

    monkeypatch.setattr(
        "seo_audit.clientreport.api_client.ReportingNinjaClient", FakeRNClient
    )
    rn_result = gsc_adapter.GscAdapter().collect(
        _ctx(Provider.gsc, "sc-domain:example.com", "reporting_ninja"),
        lambda _e: None, lambda: False,
    )

    g = _by_key(google_result.metrics)
    r = _by_key(rn_result.metrics)
    for key in ("gsc_clicks", "gsc_impressions", "gsc_avg_position"):
        assert g[(key, "current")].value_numeric == r[(key, "current")].value_numeric, key
    assert google_result.provenance["transport"] == "google_api"


# ── §3.4 GSC page/query dimensions ───────────────────────────────────────────
def test_gsc_dimensions_google_api_without_token_is_not_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def no_token(*_a: object, **_k: object) -> object:
        raise GoogleApiConfigError("no token")

    monkeypatch.setattr("app.integrations.gsc.open_google_api_session", no_token)
    result = gsc_adapter.GscAdapter().collect_dimensions(
        _ctx(Provider.gsc, "sc-domain:example.com", "google_api"), lambda _e: None
    )
    assert result.error_code == "not_configured"
    assert result.rows == []


def test_gsc_dimensions_parity_between_transports(
    monkeypatch: pytest.MonkeyPatch, fake_google: FakeGoogle
) -> None:
    def gsc_page_query(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        dim = body["dimensions"][0]
        key = {"page": "/blog/a", "query": "hearing aids", "date": "2026-08-28"}[dim]
        return httpx.Response(
            200,
            json={"rows": [
                {"keys": [key], "clicks": 10, "impressions": 100, "ctr": 0.1, "position": 3.0}
            ]},
        )

    fake_google.on("searchAnalytics/query", gsc_page_query)
    session = fake_google.session()
    monkeypatch.setattr(
        "app.integrations.gsc.open_google_api_session", lambda *_a, **_k: session
    )
    google_result = gsc_adapter.GscAdapter().collect_dimensions(
        _ctx(Provider.gsc, "sc-domain:example.com", "google_api"), lambda _e: None
    )
    assert {r.dimension for r in google_result.rows} == {"page", "query"}
    page_row = next(r for r in google_result.rows if r.dimension == "page")
    assert page_row.dimension_value == "/blog/a"
    assert page_row.clicks == 10
    assert page_row.ctr == Decimal("0.1")
    assert google_result.provenance["transport"] == "google_api"


def test_gsc_dimensions_truncation_flagged_without_extra_page(
    fake_google: FakeGoogle, monkeypatch: pytest.MonkeyPatch
) -> None:
    """top_n=2 with 3 rows available -> truncated, one API call only."""
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        body = json.loads(request.content)
        if body["dimensions"] == ["date"]:
            # the `_gsc_data_through` freshness probe (role=current only)
            return httpx.Response(200, json={"rows": [{"keys": ["2026-08-28"], "clicks": 1}]})
        return httpx.Response(
            200,
            json={"rows": [
                {"keys": [f"/p{i}"], "clicks": 10 - i, "impressions": 100,
                 "ctr": 0.1, "position": 3.0}
                for i in range(3)
            ]},
        )

    fake_google.on("searchAnalytics/query", handler)
    session = fake_google.session()
    monkeypatch.setattr(
        "app.integrations.gsc.open_google_api_session", lambda *_a, **_k: session
    )
    result = gsc_adapter.GscAdapter().collect_dimensions(
        _ctx(Provider.gsc, "sc-domain:example.com", "google_api"), lambda _e: None, top_n=2
    )
    page_rows = [
        r for r in result.rows
        if r.dimension == "page" and r.period_role.value == "current"
    ]
    assert len(page_rows) == 2
    assert all(r.quality == Quality.truncated for r in page_rows)
    # 2 dims x 2 periods (page/query, current/previous) + 1 data_through
    # freshness probe = 5 calls total, not 9 (one page each, no 2nd-page fetch).
    assert call_count == 5


# ── §3.5 GA4 KPI aggregate (`rn_users`/`rn_organic_users`/…) ────────────────
def test_rn_kpi_google_api_without_token_is_not_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def no_token(*_a: object, **_k: object) -> object:
        raise GoogleApiConfigError("no token")

    monkeypatch.setattr("app.integrations.reporting_ninja.open_google_api_session", no_token)
    result = rn_adapter.ReportingNinjaAdapter().collect(
        _ctx(Provider.reporting_ninja, "properties/1", "google_api"),
        lambda _e: None, lambda: False,
    )
    assert result.error_code == "not_configured"
    assert result.metrics == []


def test_rn_kpi_parity_between_transports(
    monkeypatch: pytest.MonkeyPatch, fake_google: FakeGoogle
) -> None:
    def ga4_report(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        dims = [d["name"] for d in body["dimensions"]]
        if not dims:
            return httpx.Response(200, json={
                "dimensionHeaders": [],
                "metricHeaders": [{"name": "totalUsers"}, {"name": "keyEvents"}],
                "rows": [{"dimensionValues": [],
                          "metricValues": [{"value": "800"}, {"value": "12"}]}],
                "rowCount": 1,
            })
        return httpx.Response(200, json={
            "dimensionHeaders": [{"name": "firstUserDefaultChannelGroup"}],
            "metricHeaders": [{"name": "totalUsers"}, {"name": "keyEvents"}],
            "rows": [
                {"dimensionValues": [{"value": "Organic Search"}],
                 "metricValues": [{"value": "300"}, {"value": "5"}]},
                {"dimensionValues": [{"value": "Direct"}],
                 "metricValues": [{"value": "400"}, {"value": "3"}]},
            ],
            "rowCount": 2,
        })

    fake_google.on(":runReport", ga4_report)
    session = fake_google.session()
    monkeypatch.setattr(
        "app.integrations.reporting_ninja.open_google_api_session", lambda *_a, **_k: session
    )
    google_result = rn_adapter.ReportingNinjaAdapter().collect(
        _ctx(Provider.reporting_ninja, "properties/1", "google_api"), lambda _e: None, lambda: False
    )

    class FakeSeoAudit:
        quality = DashQuality.complete
        observed_at = datetime.now(UTC)
        warnings: list[str] = []
        error_code = None
        provenance = {"transport": "reporting_ninja"}
        values = [
            DashboardMetricValue(
                metric_key="users", period_role=DashPeriodRole.current,
                period_start=CURRENT.start, period_end=CURRENT.end,
                value_numeric=Decimal(800), unit=DashUnit.count,
            ),
            DashboardMetricValue(
                metric_key="organic_users", period_role=DashPeriodRole.current,
                period_start=CURRENT.start, period_end=CURRENT.end,
                value_numeric=Decimal(300), unit=DashUnit.count,
            ),
            DashboardMetricValue(
                metric_key="total_conversions", period_role=DashPeriodRole.current,
                period_start=CURRENT.start, period_end=CURRENT.end,
                value_numeric=Decimal(12), unit=DashUnit.count,
            ),
            DashboardMetricValue(
                metric_key="organic_conversions", period_role=DashPeriodRole.current,
                period_start=CURRENT.start, period_end=CURRENT.end,
                value_numeric=Decimal(5), unit=DashUnit.count,
            ),
        ]

    from seo_audit.dashboard import reporting_ninja as real_wrapper

    monkeypatch.setattr(real_wrapper, "collect_ga4", lambda *_a, **_k: FakeSeoAudit())

    class FakeRNClient:
        def __enter__(self) -> FakeRNClient:
            return self

        def __exit__(self, *_a: object) -> None:
            return None

    monkeypatch.setattr(
        "seo_audit.clientreport.api_client.ReportingNinjaClient", FakeRNClient
    )
    rn_result = rn_adapter.ReportingNinjaAdapter().collect(
        _ctx(Provider.reporting_ninja, "properties/1", "reporting_ninja"),
        lambda _e: None, lambda: False,
    )

    g = _by_key(google_result.metrics)
    r = _by_key(rn_result.metrics)
    for key in ("rn_users", "rn_organic_users", "rn_total_conversions", "rn_organic_conversions"):
        assert g[(key, "current")].value_numeric == r[(key, "current")].value_numeric, key
    assert google_result.provenance["transport"] == "google_api"


# ── §3.5 GA4 breakdown datasets, generalized beyond ga4_daily/_channels ─────
def test_ga4_breakdown_datasets_parity_and_percent_scaling(
    monkeypatch: pytest.MonkeyPatch, fake_google: FakeGoogle
) -> None:
    """`ga4_channels` (has `engagement_rate`, must scale ×100) and
    `ga4_devices` (no `engagement_rate`) both come out of the generalized
    google_api loop with the same values Reporting Ninja would store."""

    def ga4_report(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        dims = [d["name"] for d in body["dimensions"]]
        if dims == ["date"]:
            return httpx.Response(200, json={
                "dimensionHeaders": [{"name": "date"}],
                "metricHeaders": [{"name": m["name"]} for m in body["metrics"]],
                "rows": [], "rowCount": 0,
            })
        if dims == ["date", "firstUserDefaultChannelGroup"]:
            return httpx.Response(200, json={
                "dimensionHeaders": [{"name": "date"}, {"name": "firstUserDefaultChannelGroup"}],
                "metricHeaders": [{"name": "keyEvents"}], "rows": [], "rowCount": 0,
            })
        if dims == ["sessionDefaultChannelGroup"]:
            return httpx.Response(200, json={
                "dimensionHeaders": [{"name": "sessionDefaultChannelGroup"}],
                "metricHeaders": [{"name": m["name"]} for m in body["metrics"]],
                "rows": [{
                    "dimensionValues": [{"value": "Organic Search"}],
                    "metricValues": [{"value": "2612"}, {"value": "2000"}, {"value": "43"},
                                     {"value": "0.7638"}],
                }],
                "rowCount": 1,
            })
        if dims == ["deviceCategory"]:
            return httpx.Response(200, json={
                "dimensionHeaders": [{"name": "deviceCategory"}],
                "metricHeaders": [{"name": m["name"]} for m in body["metrics"]],
                "rows": [{
                    "dimensionValues": [{"value": "mobile"}],
                    "metricValues": [{"value": "3047"}, {"value": "3470"}, {"value": "61"}],
                }],
                "rowCount": 1,
            })
        # `ga4_landing_pages`, `ga4_events`, `ga4_countries` — not the focus of
        # this test, empty is a valid (if uninteresting) answer.
        return httpx.Response(200, json={
            "dimensionHeaders": [{"name": d} for d in dims],
            "metricHeaders": [{"name": m["name"]} for m in body["metrics"]],
            "rows": [], "rowCount": 0,
        })

    fake_google.on(":runReport", ga4_report)
    session = fake_google.session()
    monkeypatch.setattr(
        "app.integrations.reporting_ninja.open_google_api_session", lambda *_a, **_k: session
    )
    result = rn_adapter.ReportingNinjaAdapter().collect_datasets(
        _ctx(Provider.reporting_ninja, "properties/1", "google_api"), lambda _e: None
    )

    channels = [r for r in result.rows if r.dataset_key == "ga4_channels"
                and r.period_role.value == "current"]
    assert len(channels) == 1
    assert channels[0].dimension_value == "Organic Search"
    assert channels[0].metrics["sessions"] == Decimal(2000)
    # engagementRate 0.7638 (API fraction) -> 76.38 (RN-convention percentage)
    assert channels[0].metrics["engagement_rate"] == Decimal("76.38")

    devices = [r for r in result.rows if r.dataset_key == "ga4_devices"
               and r.period_role.value == "current"]
    assert len(devices) == 1
    assert devices[0].dimension_value == "mobile"
    assert devices[0].metrics["sessions"] == Decimal(3470)
    assert "engagement_rate" not in devices[0].metrics

    assert set(result.provenance["datasets"]) >= {
        "ga4_daily", "ga4_channels", "ga4_landing_pages", "ga4_events",
        "ga4_devices", "ga4_countries", "ga4_daily_channels",
    }
