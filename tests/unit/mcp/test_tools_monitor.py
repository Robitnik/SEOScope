"""Група C — site monitor: list_site_changes, diff_monitored_page."""

from __future__ import annotations

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from app.mcp.server import mcp

_CLIENTS = [{"id": "c1", "name": "Alpha Corp", "domain": "alpha.example.com", "slug": "alpha"}]

_CHANGE = {
    "id": "mc1",
    "client_id": "c1",
    "run_id": "r1",
    "severity": "warning",
    "change_type": "title",
    "scope": "page",
    "subject": "/pricing",
    "related_id": "p1",
    "old_value": "Old title",
    "new_value": "New title",
    "details": {},
    "detected_at": "2026-09-05T00:00:00Z",
    "acknowledged_at": None,
}


@pytest.mark.asyncio
async def test_list_site_changes_portfolio_wide_attaches_client_names(fake_api) -> None:
    fake_api.json("GET", "/api/v1/monitor/changes", [_CHANGE])
    fake_api.json("GET", "/api/v1/clients", _CLIENTS)

    result = await mcp.call_tool("list_site_changes", {})
    assert not result.is_error
    items = result.structured_content["items"]
    assert items[0]["client_name"] == "Alpha Corp"


@pytest.mark.asyncio
async def test_list_site_changes_scoped_to_one_client(fake_api) -> None:
    fake_api.json("GET", "/api/v1/clients", _CLIENTS)
    fake_api.json("GET", "/api/v1/clients/c1/monitor/changes", [_CHANGE])

    result = await mcp.call_tool("list_site_changes", {"client": "Alpha"})
    assert not result.is_error
    assert result.structured_content["items"][0]["client_name"] == "Alpha Corp"


@pytest.mark.asyncio
async def test_list_site_changes_filters_out_stale_rows_client_side(fake_api) -> None:
    old_change = {**_CHANGE, "detected_at": "2020-01-01T00:00:00Z"}
    fake_api.json("GET", "/api/v1/monitor/changes", [old_change])
    fake_api.json("GET", "/api/v1/clients", _CLIENTS)

    result = await mcp.call_tool("list_site_changes", {"days": 7})
    assert not result.is_error
    assert result.structured_content == {"total": 0, "items": []}


@pytest.mark.asyncio
async def test_list_site_changes_caps_items_but_keeps_the_true_total(fake_api) -> None:
    # The client-scoped endpoint has no `limit` of its own — found live
    # against real data (a busy client returned 100 open changes in one
    # response). Newest first, capped at 50, `total` stays the real count.
    rows = [
        {**_CHANGE, "id": f"mc{i}", "detected_at": f"2026-09-{(i % 5) + 1:02d}T00:00:00Z"}
        for i in range(70)
    ]
    fake_api.json("GET", "/api/v1/clients", _CLIENTS)
    fake_api.json("GET", "/api/v1/clients/c1/monitor/changes", rows)

    result = await mcp.call_tool("list_site_changes", {"client": "Alpha", "days": 30})
    body = result.structured_content
    assert body["total"] == 70
    assert len(body["items"]) == 50
    dates = [item["detected_at"] for item in body["items"]]
    assert dates == sorted(dates, reverse=True)


@pytest.mark.asyncio
async def test_diff_monitored_page(fake_api) -> None:
    fake_api.json("GET", "/api/v1/clients", _CLIENTS)
    fake_api.json(
        "GET",
        "/api/v1/clients/c1/monitor/pages",
        {
            "pages": [
                {
                    "id": "p1",
                    "url": "https://alpha.example.com/pricing",
                    "is_key_page": True,
                    "in_sitemap": True,
                    "status": 200,
                    "final_url": None,
                    "indexable": True,
                    "meta_robots": None,
                    "x_robots_tag": None,
                    "title": "Pricing",
                    "canonical": None,
                    "version_seq": 2,
                    "gone_since": None,
                    "consecutive_failures": 0,
                    "last_checked_at": "2026-09-05T00:00:00Z",
                }
            ],
            "total": 1,
            "limit": 20,
            "offset": 0,
        },
    )
    fake_api.json(
        "GET",
        "/api/v1/clients/c1/monitor/pages/p1/diff",
        {
            "from_version_id": "v1",
            "to_version_id": "v2",
            "changed_fields": ["title"],
            "from_signals": {},
            "to_signals": {},
            "text_diff": ["-Old title", "+New title"],
        },
    )

    result = await mcp.call_tool("diff_monitored_page", {"client": "Alpha", "page": "pricing"})
    assert not result.is_error
    assert result.structured_content["changed_fields"] == ["title"]


@pytest.mark.asyncio
async def test_diff_monitored_page_no_matching_page_is_a_tool_error(fake_api) -> None:
    fake_api.json("GET", "/api/v1/clients", _CLIENTS)
    fake_api.json(
        "GET",
        "/api/v1/clients/c1/monitor/pages",
        {"pages": [], "total": 0, "limit": 20, "offset": 0},
    )
    with pytest.raises(ToolError, match="No monitored page matches"):
        await mcp.call_tool("diff_monitored_page", {"client": "Alpha", "page": "nowhere"})
