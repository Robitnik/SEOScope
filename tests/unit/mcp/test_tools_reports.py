"""Група D+F — звіти: monthly performance, report table, run_client_report."""

from __future__ import annotations

import pytest

from app.mcp.server import mcp

_CLIENTS = [{"id": "c1", "name": "Alpha Corp", "domain": "alpha.example.com", "slug": "alpha"}]


def _month(month: str, users: int) -> dict:
    return {
        "month": month,
        "label": month,
        "days_in_month": 30,
        "organic_users": users,
        "organic_conversions": 10,
        "key_event_count": 10,
        "gsc_clicks": 100,
        "gsc_impressions": 1000,
        "gsc_days_with_data": 30,
        "conversion_rate": 10.0,
        "ctr": 10.0,
        "is_partial_period": False,
        "ga4_state": "ok",
        "gsc_state": "ok",
        "truncated": False,
        "directions": {"organic_users": "up"},
    }


@pytest.mark.asyncio
async def test_get_client_monthly_performance_caps_to_most_recent_months(fake_api) -> None:
    fake_api.json("GET", "/api/v1/clients", _CLIENTS)
    rows = [_month(m, i) for i, m in enumerate(["2026-05", "2026-06", "2026-07", "2026-08"])]
    fake_api.json(
        "GET",
        "/api/v1/clients/c1/monthly-table",
        {"client_id": "c1", "rows": rows, "settings": {}, "coverage": {}, "collected_at": None},
    )

    result = await mcp.call_tool(
        "get_client_monthly_performance", {"client": "Alpha", "months": 2}
    )
    assert not result.is_error
    months = [r["month"] for r in result.structured_content["rows"]]
    assert months == ["2026-07", "2026-08"]
    # `better_direction` semantics stay whatever the API already resolved.
    assert result.structured_content["rows"][0]["directions"] == {"organic_users": "up"}


@pytest.mark.asyncio
async def test_get_report_data_carries_metric_key_for_catalog_cross_reference(fake_api) -> None:
    fake_api.json(
        "GET",
        "/api/v1/reports/runs/run1/table",
        {
            "run_id": "run1",
            "kind": "monthly",
            "source": "api",
            "periods": [
                {
                    "label": "Aug 2026",
                    "date_from": "2026-08-01",
                    "date_to": "2026-08-31",
                    "is_partial": False,
                }
            ],
            "change_labels": ["MoM"],
            "rows": [
                {"key": "gsc_clicks", "label": "GSC Clicks", "values": [100], "changes": [-5.0]}
            ],
            "sources": {},
            "cross_check": None,
        },
    )
    result = await mcp.call_tool("get_report_data", {"run_id": "run1"})
    assert not result.is_error
    row = result.structured_content["rows"][0]
    assert row["metric_key"] == "gsc_clicks"
    assert "better_direction" in result.structured_content["note"]


@pytest.mark.asyncio
async def test_run_client_report_returns_a_pollable_job(fake_api) -> None:
    fake_api.json("GET", "/api/v1/clients", _CLIENTS)
    fake_api.json(
        "POST",
        "/api/v1/reports/runs",
        {
            "run_id": "run1",
            "status_url": "/api/v1/reports/runs/run1",
            "events_url": "/api/v1/reports/runs/run1/events",
        },
    )

    result = await mcp.call_tool(
        "run_client_report", {"client": "Alpha", "kind": "monthly"}
    )
    assert not result.is_error
    assert result.structured_content == {
        "run_id": "run1",
        "poll_with": "get_job_status",
        "kind": "report_run",
    }

    method, path, _ = fake_api.calls[-1]
    assert (method, path) == ("POST", "/api/v1/reports/runs")
