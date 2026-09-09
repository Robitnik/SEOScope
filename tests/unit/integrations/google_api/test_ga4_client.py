"""Ga4Client — pagination by offset, date normalization, generic row shape (task-83 §10.3)."""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

import httpx
import pytest

from app.integrations.google_api import ga4_client as ga4_module
from app.integrations.google_api.ga4_client import Ga4Client

from .fakes import FakeGoogle

PROP = "properties/402129482"
START = date(2026, 8, 1)
END = date(2026, 8, 31)


def _report(rows: list[dict[str, object]], *, row_count: int | None = None) -> dict[str, object]:
    return {
        "dimensionHeaders": [{"name": "date"}],
        "metricHeaders": [{"name": "sessions"}, {"name": "keyEvents"}],
        "rows": rows,
        "rowCount": row_count if row_count is not None else len(rows),
    }


def _grow(day: str, sessions: str, key_events: str) -> dict[str, object]:
    return {
        "dimensionValues": [{"value": day}],
        "metricValues": [{"value": sessions}, {"value": key_events}],
    }


def test_run_report_normalizes_yyyymmdd_dates(fake_google: FakeGoogle) -> None:
    fake_google.on(
        ":runReport", httpx.Response(200, json=_report([_grow("20260801", "10", "2")]))
    )
    rows = Ga4Client(fake_google.session()).run_report(
        PROP, START, END, dimensions=["date"], metrics=["sessions", "keyEvents"]
    )
    assert rows[0].dimensions == {"date": "2026-08-01"}
    assert rows[0].metrics == {"sessions": Decimal(10), "keyEvents": Decimal(2)}


def test_run_report_pages_by_offset(
    fake_google: FakeGoogle, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ga4_module, "_PAGE_LIMIT", 2)
    seen_offsets: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        offset = json.loads(request.content)["offset"]
        seen_offsets.append(offset)
        page = [_grow(f"2026080{offset + i + 1}", "1", "1") for i in range(2 if offset < 4 else 1)]
        return httpx.Response(200, json=_report(page, row_count=5))

    fake_google.on(":runReport", handler)
    rows = Ga4Client(fake_google.session()).run_report(
        PROP, START, END, dimensions=["date"], metrics=["sessions", "keyEvents"]
    )
    assert seen_offsets == [0, 2, 4]
    assert len(rows) == 5


def test_body_requests_key_events_only_no_conversions(fake_google: FakeGoogle) -> None:
    fake_google.on(":runReport", httpx.Response(200, json=_report([])))
    Ga4Client(fake_google.session()).run_report(
        PROP, START, END, dimensions=["date"], metrics=["totalUsers", "keyEvents"]
    )
    body = json.loads(fake_google.requests[-1].content)
    metric_names = {m["name"] for m in body["metrics"]}
    assert "keyEvents" in metric_names
    assert "conversions" not in metric_names
    assert body["dimensions"] == [{"name": "date"}]


def test_absent_day_stays_absent(fake_google: FakeGoogle) -> None:
    """GA4 omits days with no activity; the client must not synthesize a zero
    row for them (PLAN §12)."""
    fake_google.on(
        ":runReport",
        httpx.Response(
            200, json=_report([_grow("20260801", "5", "1"), _grow("20260803", "7", "0")])
        ),
    )
    rows = Ga4Client(fake_google.session()).run_report(
        PROP, START, END, dimensions=["date"], metrics=["sessions", "keyEvents"]
    )
    days = {r.dimensions["date"] for r in rows}
    assert days == {"2026-08-01", "2026-08-03"}
    # a real zero is preserved
    aug3 = next(r for r in rows if r.dimensions["date"] == "2026-08-03")
    assert aug3.metrics["keyEvents"] == Decimal(0)


# ── task-89 §3.3: `dimension_filter` and `limit` ────────────────────────────
def test_dimension_filter_passed_through_verbatim(fake_google: FakeGoogle) -> None:
    fake_google.on(":runReport", httpx.Response(200, json=_report([])))
    dim_filter = {
        "filter": {"fieldName": "eventName", "inListFilter": {"values": ["Phone", "Email"]}}
    }
    Ga4Client(fake_google.session()).run_report(
        PROP, START, END, dimensions=["eventName"], metrics=["keyEvents"],
        dimension_filter=dim_filter,
    )
    body = json.loads(fake_google.requests[-1].content)
    assert body["dimensionFilter"] == dim_filter


def test_no_dimension_filter_key_when_not_passed(fake_google: FakeGoogle) -> None:
    fake_google.on(":runReport", httpx.Response(200, json=_report([])))
    Ga4Client(fake_google.session()).run_report(
        PROP, START, END, dimensions=["date"], metrics=["sessions"]
    )
    body = json.loads(fake_google.requests[-1].content)
    assert "dimensionFilter" not in body


def test_limit_bounds_total_rows_across_pages(
    fake_google: FakeGoogle, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`limit` caps the TOTAL row count the client hands back, independent of
    the per-page `_PAGE_LIMIT` (task-89 §3.3) — mirrors
    `_GA4_DATASETS[...]["limit"]` so a truncated Google API breakdown can
    flag `Quality.truncated` at the same threshold the Reporting Ninja path
    already uses."""
    monkeypatch.setattr(ga4_module, "_PAGE_LIMIT", 3)
    seen_offsets: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        offset = json.loads(request.content)["offset"]
        seen_offsets.append(offset)
        page = [_grow(f"2026080{offset + i + 1}", "1", "1") for i in range(3)]
        return httpx.Response(200, json=_report(page, row_count=10))

    fake_google.on(":runReport", handler)
    rows = Ga4Client(fake_google.session()).run_report(
        PROP, START, END, dimensions=["date"], metrics=["sessions", "keyEvents"], limit=5,
    )
    assert len(rows) == 5
    # Truncated mid-page (limit=5 falls inside the 2nd page of 3) — the next
    # page must never be requested once the limit is already satisfied.
    assert seen_offsets == [0, 3]


def test_limit_none_is_unbounded_default(fake_google: FakeGoogle) -> None:
    fake_google.on(
        ":runReport", httpx.Response(200, json=_report([_grow("20260801", "1", "1")]))
    )
    rows = Ga4Client(fake_google.session()).run_report(
        PROP, START, END, dimensions=["date"], metrics=["sessions", "keyEvents"]
    )
    assert len(rows) == 1


def test_two_dimension_rows_carry_both(fake_google: FakeGoogle) -> None:
    fake_google.on(
        ":runReport",
        httpx.Response(
            200,
            json={
                "dimensionHeaders": [{"name": "date"}, {"name": "firstUserDefaultChannelGroup"}],
                "metricHeaders": [{"name": "keyEvents"}],
                "rows": [
                    {
                        "dimensionValues": [{"value": "20260801"}, {"value": "Organic Search"}],
                        "metricValues": [{"value": "12"}],
                    }
                ],
                "rowCount": 1,
            },
        ),
    )
    rows = Ga4Client(fake_google.session()).run_report(
        PROP, START, END,
        dimensions=["date", "firstUserDefaultChannelGroup"],
        metrics=["keyEvents"],
    )
    assert rows[0].dimensions == {
        "date": "2026-08-01",
        "firstUserDefaultChannelGroup": "Organic Search",
    }
    assert rows[0].metrics == {"keyEvents": Decimal(12)}
