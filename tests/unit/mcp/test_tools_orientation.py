"""Група A — list_clients, get_client_overview."""

from __future__ import annotations

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from app.mcp.server import mcp

_CLIENTS = [
    {
        "id": "c1",
        "name": "Alpha Corp",
        "domain": "alpha.example.com",
        "slug": "alpha",
        "status": "active",
        "is_dev": False,
    }
]

_DASHBOARD = {
    "clients": [
        {
            "client_id": "c1",
            "name": "Alpha Corp",
            "status": "active",
            "overall_status": "ok",
            "last_synced_at": None,
            "providers": [
                {
                    "provider": "gsc",
                    "state": "ok",
                    "metrics": [
                        {
                            "metric_key": "gsc_clicks",
                            "current_text": "1,234",
                            "delta_pct": 12.5,
                            "is_improvement": True,
                        }
                    ],
                }
            ],
            "health": {"value": 72.5, "coverage_contributing": 2, "coverage_configured": 3},
        }
    ],
    "generated_at": "2026-09-06T00:00:00Z",
}


@pytest.mark.asyncio
async def test_list_clients(fake_api) -> None:
    fake_api.json("GET", "/api/v1/clients", _CLIENTS)
    result = await mcp.call_tool("list_clients", {})
    assert not result.is_error
    rows = result.structured_content["result"]
    assert rows == [
        {
            "client_id": "c1",
            "name": "Alpha Corp",
            "domain": "alpha.example.com",
            "status": "active",
            "is_dev": False,
        }
    ]


@pytest.mark.asyncio
async def test_get_client_overview_carries_precomputed_direction(fake_api) -> None:
    fake_api.json("GET", "/api/v1/clients", _CLIENTS)
    fake_api.json("GET", "/api/v1/dashboard", _DASHBOARD)

    result = await mcp.call_tool("get_client_overview", {"client": "Alpha"})
    assert not result.is_error
    card = result.structured_content
    assert card["client_id"] == "c1"
    assert card["overall_status"] == "ok"
    # `is_improvement` travels with the metric — the model never has to
    # infer good/bad from the raw sign of `delta_pct` for this tool.
    assert card["metrics"]["gsc_clicks"]["is_improvement"] is True
    assert card["metrics"]["health_score"] == 72.5
    assert card["metrics"]["health_coverage"] == "2/3"


@pytest.mark.asyncio
async def test_get_client_overview_unknown_client_is_a_tool_error(fake_api) -> None:
    fake_api.json("GET", "/api/v1/clients", _CLIENTS)
    with pytest.raises(ToolError, match="not found"):
        await mcp.call_tool("get_client_overview", {"client": "Nonexistent"})
