"""Transport resolution + row-shape parity — the heart of task-83 (§10.5/§10.6)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from decimal import Decimal

import httpx
import pytest

from app.integrations import gsc as gsc_adapter
from app.integrations import reporting_ninja as rn_adapter
from app.integrations.google_api import resolve_transport
from app.schemas.adapter import CollectionContext, DateRange
from app.schemas.enums import ComparisonMode, PeriodKind, Provider, Quality, SourceMode

from .fakes import FakeGoogle

CURRENT = DateRange(start=date(2026, 8, 1), end=date(2026, 8, 3))
PREVIOUS = DateRange(start=date(2026, 7, 29), end=date(2026, 7, 31))


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


# ── §10.5 resolution ────────────────────────────────────────────────────────
def test_explicit_config_beats_global_default() -> None:
    class S:
        google_api_default_transport = "reporting_ninja"

    assert resolve_transport({"transport": "google_api"}, settings=S()) == "google_api"  # type: ignore[arg-type]


def test_global_default_applies_when_config_silent() -> None:
    class S:
        google_api_default_transport = "google_api"

    assert resolve_transport({}, settings=S()) == "google_api"  # type: ignore[arg-type]
    assert resolve_transport({"transport": "  "}, settings=S()) == "google_api"  # type: ignore[arg-type]


def test_garbage_falls_back_to_reporting_ninja() -> None:
    class S:
        google_api_default_transport = "nonsense"

    assert resolve_transport({"transport": "carrier-pigeon"}, settings=S()) == "reporting_ninja"  # type: ignore[arg-type]


def test_google_api_without_token_is_not_configured_never_silent_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`transport=google_api` + empty refresh token => `not_configured` with a
    warning, NOT a quiet switch back to Reporting Ninja (task-83 §6.1.3)."""
    from app.integrations.google_api.errors import GoogleApiConfigError

    def no_token(*_a: object, **_k: object) -> object:
        raise GoogleApiConfigError("google_api_refresh_token must be set")

    monkeypatch.setattr("app.integrations.gsc.open_google_api_session", no_token)

    def rn_must_not_run(*_a: object, **_k: object) -> object:
        raise AssertionError("Reporting Ninja must not be a silent fallback")

    monkeypatch.setattr(
        "seo_audit.clientreport.api_client.ReportingNinjaClient", rn_must_not_run
    )

    result = gsc_adapter.GscAdapter()._collect_datasets_google_api(
        _ctx(Provider.gsc, "sc-domain:example.com", "google_api"), lambda _e: None
    )
    assert result.error_code == "not_configured"
    assert result.quality == Quality.unavailable
    assert any("not configured" in w for w in result.warnings)
    assert result.rows == []


# ── §10.6 row-shape parity: GSC ─────────────────────────────────────────────
def test_gsc_daily_row_parity_between_transports(
    monkeypatch: pytest.MonkeyPatch, fake_google: FakeGoogle
) -> None:
    def gsc_page(request: httpx.Request) -> httpx.Response:
        import json

        start = json.loads(request.content)["startDate"]
        # ctr as the API's own 0..1 fraction
        return httpx.Response(
            200,
            json={
                "rows": [
                    {"keys": [start], "clicks": 100, "impressions": 1000,
                     "ctr": 0.1, "position": 4.5},
                ]
            },
        )

    fake_google.on("searchAnalytics/query", gsc_page)
    session = fake_google.session()
    monkeypatch.setattr(
        "app.integrations.gsc.open_google_api_session", lambda *_a, **_k: session
    )

    google_result = gsc_adapter.GscAdapter().collect_datasets(
        _ctx(Provider.gsc, "sc-domain:example.com", "google_api"), lambda _e: None
    )

    class FakeRN:
        def __enter__(self) -> FakeRN:
            return self

        def __exit__(self, *_a: object) -> None:
            return None

        def query_all(self, *_a: object, **kwargs: object) -> dict[str, object]:
            start = _a[4]
            # Reporting Ninja returns ctr as a PERCENTAGE (10 == 10%)
            return {
                "rows": [
                    {"date": start, "clicks": 100, "impressions": 1000,
                     "ctr": 10, "avgposition": 4.5}
                ],
                "truncated": False,
            }

    monkeypatch.setattr("seo_audit.clientreport.api_client.ReportingNinjaClient", FakeRN)
    monkeypatch.setattr(
        "seo_audit.clientreport.gsc_fetcher.discover_gsc_fields",
        lambda _c: {"clicks": "clicks", "impressions": "impressions",
                    "ctr": "ctr", "avg_position": "avgposition"},
    )
    rn_result = gsc_adapter.GscAdapter().collect_datasets(
        _ctx(Provider.gsc, "sc-domain:example.com", "reporting_ninja"), lambda _e: None
    )

    _assert_rows_equal(google_result.rows, rn_result.rows)
    assert google_result.provenance["transport"] == "google_api"
    assert rn_result.provenance["transport"] == "reporting_ninja"
    # the ×100 guard: both stored ctr as the same 0..1 fraction
    a_ctr = google_result.rows[0].metrics["ctr"]
    assert a_ctr == Decimal("0.1")


# ── §10.6 row-shape parity: GA4 daily + channels ────────────────────────────
def test_ga4_trends_row_parity_between_transports(
    monkeypatch: pytest.MonkeyPatch, fake_google: FakeGoogle
) -> None:
    import json

    # The GA4 Data API returns `engagementRate` as a 0..1 fraction; Reporting
    # Ninja returns it as a percentage. Both stores must land on the same
    # number — that is what this parity test guards.
    def ga4_report(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        dims = [d["name"] for d in body["dimensions"]]
        start = body["dateRanges"][0]["startDate"].replace("-", "")
        if dims == ["date"]:
            vals = {"totalUsers": "800", "sessions": "1000", "keyEvents": "12",
                    "engagementRate": "0.765"}
            return httpx.Response(200, json={
                "dimensionHeaders": [{"name": "date"}],
                "metricHeaders": [{"name": m["name"]} for m in body["metrics"]],
                "rows": [{
                    "dimensionValues": [{"value": start}],
                    "metricValues": [{"value": vals[m["name"]]} for m in body["metrics"]],
                }],
                "rowCount": 1,
            })
        return httpx.Response(200, json={
            "dimensionHeaders": [{"name": "date"}, {"name": "firstUserDefaultChannelGroup"}],
            "metricHeaders": [{"name": "keyEvents"}],
            "rows": [
                {"dimensionValues": [{"value": start}, {"value": "Organic Search"}],
                 "metricValues": [{"value": "8"}]},
                {"dimensionValues": [{"value": start}, {"value": "Direct"}],
                 "metricValues": [{"value": "3"}]},
            ],
            "rowCount": 2,
        })

    fake_google.on(":runReport", ga4_report)
    session = fake_google.session()
    monkeypatch.setattr(
        "app.integrations.reporting_ninja.open_google_api_session", lambda *_a, **_k: session
    )

    all_fields = set()
    for definition in rn_adapter._GA4_DATASETS.values():
        all_fields.add(definition["dimension"])
        all_fields.update(definition["metrics"].values())
    all_fields.add(rn_adapter._CHANNEL_DIMENSION)

    class FakeRN:
        def __enter__(self) -> FakeRN:
            return self

        def __exit__(self, *_a: object) -> None:
            return None

        def list_fields(self, _integration: str) -> list[dict[str, str]]:
            return [{"field_id": f} for f in all_fields]

        def query_all(self, _integration: str, _ck: str, _aid: str, fields: list[str],
                      start: str, _end: str, **_kw: object) -> dict[str, object]:
            day = start.replace("-", "")
            channel_dim = rn_adapter._CHANNEL_DIMENSION
            if channel_dim in fields:
                return {"rows": [
                    {"date": day, channel_dim: "Organic Search", "keyEvents": "8"},
                    {"date": day, channel_dim: "Direct", "keyEvents": "3"},
                ], "truncated": False}
            # Reporting Ninja's own scaling: engagementRate as a percentage.
            rn_vals = {"totalUsers": "800", "sessions": "1000", "keyEvents": "12",
                       "engagementRate": "76.5", "screenPageViews": "1", "eventCount": "1"}
            row = {fields[0]: day}
            for f in fields[1:]:
                row[f] = rn_vals.get(f, "1")
            return {"rows": [row], "truncated": False}

    monkeypatch.setattr("seo_audit.clientreport.api_client.ReportingNinjaClient", FakeRN)

    google_result = rn_adapter.ReportingNinjaAdapter().collect_datasets(
        _ctx(Provider.reporting_ninja, "properties/1", "google_api"), lambda _e: None
    )
    rn_result = rn_adapter.ReportingNinjaAdapter().collect_datasets(
        _ctx(Provider.reporting_ninja, "properties/1", "reporting_ninja"), lambda _e: None
    )

    for dataset_key in ("ga4_daily", "ga4_daily_channels"):
        g = [r for r in google_result.rows if r.dataset_key == dataset_key]
        n = [r for r in rn_result.rows if r.dataset_key == dataset_key]
        _assert_rows_equal(g, n)

    assert google_result.provenance["trends_ga4_transport"] == "google_api"
    assert rn_result.provenance["transport"] == "reporting_ninja"


def _assert_rows_equal(a: Sequence[object], b: Sequence[object]) -> None:
    def key(r: object) -> tuple[str, str, str]:
        return (r.dataset_key, r.dimension_value, r.period_role.value)  # type: ignore[attr-defined]

    a_by = {key(r): r for r in a}
    b_by = {key(r): r for r in b}
    assert set(a_by) == set(b_by), f"row keys differ: {set(a_by) ^ set(b_by)}"
    assert a_by, "no rows to compare"
    for k, ra in a_by.items():
        rb = b_by[k]
        assert ra.dimension_key == rb.dimension_key  # type: ignore[attr-defined]
        assert ra.period_start == rb.period_start  # type: ignore[attr-defined]
        assert ra.period_end == rb.period_end  # type: ignore[attr-defined]
        assert ra.rank == rb.rank  # type: ignore[attr-defined]
        assert dict(ra.metrics) == dict(rb.metrics), (  # type: ignore[attr-defined]
            f"{k}: {ra.metrics} != {rb.metrics}"  # type: ignore[attr-defined]
        )


# ── §10.3 channel pivot + None vs 0 ─────────────────────────────────────────
def test_channel_pivot_absent_is_none_real_zero_kept(
    monkeypatch: pytest.MonkeyPatch, fake_google: FakeGoogle
) -> None:
    from app.integrations.google_api import Ga4Client
    from app.integrations.reporting_ninja import query_ga4_daily_channels_via_google_api

    fake_google.on(":runReport", httpx.Response(200, json={
        "dimensionHeaders": [{"name": "date"}, {"name": "firstUserDefaultChannelGroup"}],
        "metricHeaders": [{"name": "keyEvents"}],
        "rows": [
            {"dimensionValues": [{"value": "20260801"}, {"value": "Organic Search"}],
             "metricValues": [{"value": "12"}]},
            # 08-02: Direct present with a real 0, Organic absent
            {"dimensionValues": [{"value": "20260802"}, {"value": "Direct"}],
             "metricValues": [{"value": "0"}]},
            # Paid Search is not tracked -> ignored, but 08-02 still "seen"
            {"dimensionValues": [{"value": "20260802"}, {"value": "Paid Search"}],
             "metricValues": [{"value": "99"}]},
        ],
        "rowCount": 3,
    }))
    rows = query_ga4_daily_channels_via_google_api(
        Ga4Client(fake_google.session()),
        account_id="properties/1",
        start=date(2026, 8, 1),
        end=date(2026, 8, 2),
    )
    by_day = dict(rows)
    assert by_day["2026-08-01"] == {"conversions_organic": Decimal(12), "conversions_direct": None}
    assert by_day["2026-08-02"] == {"conversions_organic": None, "conversions_direct": Decimal(0)}
    assert "2026-08-03" not in by_day  # a day the API never returned


def test_ga4_daily_engagement_rate_scaled_to_percent(
    fake_google: FakeGoogle,
) -> None:
    """The API returns `engagementRate` as a 0..1 fraction; every stored
    `ga4_daily` row (Reporting Ninja's convention) is a percentage, so the
    google_api branch scales ×100 for parity (task-83 §7, found in G2)."""
    from app.integrations.google_api import Ga4Client
    from app.integrations.reporting_ninja import query_ga4_daily_via_google_api

    fake_google.on(":runReport", httpx.Response(200, json={
        "dimensionHeaders": [{"name": "date"}],
        "metricHeaders": [{"name": "totalUsers"}, {"name": "sessions"},
                          {"name": "keyEvents"}, {"name": "engagementRate"}],
        "rows": [{
            "dimensionValues": [{"value": "20260801"}],
            "metricValues": [{"value": "50"}, {"value": "60"}, {"value": "7"}, {"value": "0.7654"}],
        }],
        "rowCount": 1,
    }))
    rows = query_ga4_daily_via_google_api(
        Ga4Client(fake_google.session()),
        account_id="properties/1",
        start=date(2026, 8, 1),
        end=date(2026, 8, 1),
    )
    _day, metrics = rows[0]
    assert metrics["sessions"] == Decimal(60)
    assert metrics["conversions"] == Decimal(7)
    assert metrics["engagement_rate"] == Decimal("76.54")
