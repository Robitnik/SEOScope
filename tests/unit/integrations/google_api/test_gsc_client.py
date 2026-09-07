"""GscClient — pagination, the ctr ×100 regression, permission mapping (task-83 §10.2)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import httpx
import pytest

from app.integrations.google_api import gsc_client as gsc_module
from app.integrations.google_api.errors import GoogleApiPermissionError
from app.integrations.google_api.gsc_client import GscClient

from .fakes import FakeGoogle

SITE = "sc-domain:example.com"
START = date(2026, 8, 1)
END = date(2026, 8, 31)


def _row(day: str, **kw: object) -> dict[str, object]:
    return {"keys": [day], "clicks": 10, "impressions": 100, "ctr": 0.1, "position": 3.0, **kw}


def test_daily_series_maps_fields_and_sorts(fake_google: FakeGoogle) -> None:
    fake_google.on(
        "searchAnalytics/query",
        httpx.Response(
            200,
            json={"rows": [_row("2026-08-02", clicks=5), _row("2026-08-01", clicks=9)]},
        ),
    )
    rows = GscClient(fake_google.session()).daily_search_analytics(SITE, START, END)
    assert [r.date for r in rows] == ["2026-08-01", "2026-08-02"]
    assert rows[0].clicks == Decimal(9)
    assert rows[1].clicks == Decimal(5)


def test_ctr_is_kept_as_fraction_not_multiplied_by_100(fake_google: FakeGoogle) -> None:
    """Regression: the GSC API returns ctr as a `0..1` fraction; storing it
    ×100 (as Reporting Ninja's percentage would need) inflates every value
    while still drawing a plausible graph (task-83 §4/§7.3)."""
    fake_google.on(
        "searchAnalytics/query",
        httpx.Response(200, json={"rows": [_row("2026-08-01", ctr=0.0264, position=9.17)]}),
    )
    rows = GscClient(fake_google.session()).daily_search_analytics(SITE, START, END)
    assert rows[0].ctr == Decimal("0.0264")
    assert rows[0].ctr < Decimal(1)
    assert rows[0].position == Decimal("9.17")


def test_pagination_follows_start_row(
    fake_google: FakeGoogle, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gsc_module, "_ROW_LIMIT", 2)
    seen_start_rows: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        body = json.loads(request.content)
        seen_start_rows.append(body["startRow"])
        if body["startRow"] == 0:
            return httpx.Response(200, json={"rows": [_row("2026-08-01"), _row("2026-08-02")]})
        return httpx.Response(200, json={"rows": [_row("2026-08-03")]})

    fake_google.on("searchAnalytics/query", handler)
    rows = GscClient(fake_google.session()).daily_search_analytics(SITE, START, END)
    assert seen_start_rows == [0, 2]  # stopped when page 2 came back short
    assert [r.date for r in rows] == ["2026-08-01", "2026-08-02", "2026-08-03"]


def test_site_url_is_url_encoded_whole(fake_google: FakeGoogle) -> None:
    fake_google.on("googleapis.com", httpx.Response(200, json={"rows": []}))
    GscClient(fake_google.session()).daily_search_analytics(
        "https://effyscafe.com/", START, END
    )
    url = str(fake_google.requests[-1].url)
    assert "sites/https%3A%2F%2Feffyscafe.com%2F/searchAnalytics" in url


def test_type_web_and_dates_in_body(fake_google: FakeGoogle) -> None:
    fake_google.on("searchAnalytics/query", httpx.Response(200, json={"rows": []}))
    GscClient(fake_google.session()).daily_search_analytics(SITE, START, END, search_type="web")
    import json

    body = json.loads(fake_google.requests[-1].content)
    assert body["type"] == "web"
    assert body["dimensions"] == ["date"]
    assert body["startDate"] == "2026-08-01"
    assert body["endDate"] == "2026-08-31"


def test_permission_denied_propagates(fake_google: FakeGoogle) -> None:
    fake_google.on(
        "searchAnalytics/query",
        httpx.Response(403, json={"error": {"status": "PERMISSION_DENIED", "message": "no"}}),
    )
    with pytest.raises(GoogleApiPermissionError):
        GscClient(fake_google.session()).daily_search_analytics(SITE, START, END)


def test_missing_metric_is_none_not_zero(fake_google: FakeGoogle) -> None:
    fake_google.on(
        "searchAnalytics/query",
        httpx.Response(200, json={"rows": [{"keys": ["2026-08-01"], "clicks": 4}]}),
    )
    rows = GscClient(fake_google.session()).daily_search_analytics(SITE, START, END)
    assert rows[0].clicks == Decimal(4)
    assert rows[0].impressions is None
    assert rows[0].ctr is None
