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


# ── task-89 §3.2: `search_analytics` generalization ─────────────────────────
def test_search_analytics_single_dimension(fake_google: FakeGoogle) -> None:
    fake_google.on(
        "searchAnalytics/query",
        httpx.Response(
            200,
            json={"rows": [
                {"keys": ["/blog/a"], "clicks": 5, "impressions": 50, "ctr": 0.1, "position": 3.0},
                {"keys": ["/blog/b"], "clicks": 2, "impressions": 20, "ctr": 0.1, "position": 5.0},
            ]},
        ),
    )
    rows = GscClient(fake_google.session()).search_analytics(
        SITE, START, END, dimensions=["page"]
    )
    assert [r.keys for r in rows] == [("/blog/a",), ("/blog/b",)]
    assert rows[0].clicks == Decimal(5)
    import json

    body = json.loads(fake_google.requests[-1].content)
    assert body["dimensions"] == ["page"]


def test_search_analytics_two_dimensions(fake_google: FakeGoogle) -> None:
    fake_google.on(
        "searchAnalytics/query",
        httpx.Response(
            200,
            json={"rows": [{"keys": ["/blog/a", "some query"], "clicks": 3, "impressions": 30,
                            "ctr": 0.1, "position": 2.0}]},
        ),
    )
    rows = GscClient(fake_google.session()).search_analytics(
        SITE, START, END, dimensions=["page", "query"]
    )
    assert rows[0].keys == ("/blog/a", "some query")


def test_search_analytics_dimensionless_total(fake_google: FakeGoogle) -> None:
    """`dimensions=[]` (a bare period total, task-89 §3.4) returns one row
    with an empty `keys` tuple — never crashes on the missing `keys` field."""
    fake_google.on(
        "searchAnalytics/query",
        httpx.Response(
            200,
            json={"rows": [
                {"clicks": 1736, "impressions": 281736, "ctr": 0.006, "position": 10.6}
            ]},
        ),
    )
    rows = GscClient(fake_google.session()).search_analytics(SITE, START, END, dimensions=[])
    assert rows[0].keys == ()
    assert rows[0].clicks == Decimal(1736)


def test_search_analytics_max_rows_truncates_without_extra_page(
    fake_google: FakeGoogle,
) -> None:
    """`max_rows` bounds the returned list; when the first page already has
    more than `max_rows`, the generator is never asked for a second page
    (task-89 §3.2 — the same trick GSC-dimension truncation detection
    relies on to cost one API call, not two)."""
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(200, json={"rows": [_row(f"2026-08-0{i}") for i in range(1, 6)]})

    fake_google.on("searchAnalytics/query", handler)
    rows = GscClient(fake_google.session()).search_analytics(
        SITE, START, END, dimensions=["date"], row_limit=5, max_rows=3
    )
    assert len(rows) == 3
    assert call_count == 1


def test_daily_search_analytics_still_works_via_search_analytics(
    fake_google: FakeGoogle,
) -> None:
    """Regression: `daily_search_analytics` is now a thin wrapper over
    `search_analytics` (task-89 §3.2) — task-83's callers/tests must see
    zero behaviour change."""
    fake_google.on(
        "searchAnalytics/query",
        httpx.Response(200, json={"rows": [_row("2026-08-01", clicks=7)]}),
    )
    rows = GscClient(fake_google.session()).daily_search_analytics(SITE, START, END)
    assert rows[0].date == "2026-08-01"
    assert rows[0].clicks == Decimal(7)


# ── task-89 §4.5: `sitemaps()` ───────────────────────────────────────────────
def test_sitemaps_maps_fields_and_contents(fake_google: FakeGoogle) -> None:
    fake_google.on(
        "sites/sc-domain%3Aexample.com/sitemaps",
        httpx.Response(200, json={"sitemap": [{
            "path": "https://example.com/sitemap_index.xml",
            "lastSubmitted": "2025-02-18T06:15:31.796Z",
            "isPending": False,
            "lastDownloaded": "2026-09-08T01:40:34.202Z",
            "warnings": "1",
            "errors": "0",
            "contents": [
                {"type": "web", "submitted": "248", "indexed": "0"},
                {"type": "image", "submitted": "515", "indexed": "0"},
            ],
        }]}),
    )
    entries = GscClient(fake_google.session()).sitemaps(SITE)
    assert len(entries) == 1
    entry = entries[0]
    assert entry.path == "https://example.com/sitemap_index.xml"
    assert entry.is_pending is False
    assert entry.errors == 0
    assert entry.warnings == 1
    assert entry.contents["web"] == (248, 0)
    assert entry.contents["image"] == (515, 0)


def test_sitemaps_uses_get_not_post(fake_google: FakeGoogle) -> None:
    fake_google.on("sitemaps", httpx.Response(200, json={"sitemap": []}))
    GscClient(fake_google.session()).sitemaps(SITE)
    assert fake_google.requests[-1].method == "GET"


def test_sitemaps_empty_list_is_valid(fake_google: FakeGoogle) -> None:
    fake_google.on("sitemaps", httpx.Response(200, json={}))
    assert GscClient(fake_google.session()).sitemaps(SITE) == []


def test_sitemaps_permission_denied_propagates(fake_google: FakeGoogle) -> None:
    fake_google.on(
        "sitemaps",
        httpx.Response(403, json={"error": {"status": "PERMISSION_DENIED", "message": "no"}}),
    )
    with pytest.raises(GoogleApiPermissionError):
        GscClient(fake_google.session()).sitemaps(SITE)
