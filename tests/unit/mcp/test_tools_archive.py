"""Група B — веб-архів: find/timeline/compare/changes."""

from __future__ import annotations

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from app.mcp.server import mcp

_TIMELINE = {
    "url": {
        "id": "u1",
        "client_id": "c1",
        "client_name": "Alpha Corp",
        "url": "https://alpha.example.com/pricing",
        "source": "backfill",
        "first_captured_at": "2026-01-01T00:00:00Z",
        "last_captured_at": "2026-09-01T00:00:00Z",
        "snapshot_count": 2,
        "gone_since": None,
    },
    "snapshots": [
        {
            "id": "s2",
            "ts": "20260901000000",
            "observed_at": "2026-09-01T00:00:00Z",
            "last_confirmed_at": "2026-09-01T00:00:00Z",
            "confirmed_count": 1,
            "http_status": 200,
            "store_reason": "changed",
            "title": "Pricing",
            "asset_count": 3,
            "assets_truncated": False,
            "has_change": True,
        },
        {
            "id": "s1",
            "ts": "20260801000000",
            "observed_at": "2026-08-01T00:00:00Z",
            "last_confirmed_at": "2026-08-01T00:00:00Z",
            "confirmed_count": 1,
            "http_status": 200,
            "store_reason": "first_capture",
            "title": "Pricing (old)",
            "asset_count": 3,
            "assets_truncated": False,
            "has_change": False,
        },
    ],
}

_DIFF = {
    "change": {
        "id": "ch1",
        "client_id": "c1",
        "client_name": "Alpha Corp",
        "archive_url_id": "u1",
        "url": "https://alpha.example.com/pricing",
        "snapshot_id": "s2",
        "prev_snapshot_id": "s1",
        "detected_at": "2026-09-01T00:00:00Z",
        "change_kinds": ["content"],
        "summary": {},
        "text_delta_words": 42,
    },
    "diff_lines": ["-old text", "+new text"],
}


@pytest.mark.asyncio
async def test_find_archived_url_without_client_scope(fake_api) -> None:
    fake_api.json(
        "GET",
        "/api/v1/archive/urls",
        {
            "items": [
                {
                    "id": "u1",
                    "client_id": "c1",
                    "client_name": "Alpha Corp",
                    "url": "https://alpha.example.com/pricing",
                    "snapshot_count": 3,
                    "last_captured_at": "2026-09-01T00:00:00Z",
                }
            ]
        },
    )
    result = await mcp.call_tool("find_archived_url", {"query": "pricing"})
    assert not result.is_error
    assert result.structured_content["matches"][0]["url_id"] == "u1"
    # No `client` argument was given — must not have resolved one either.
    assert not any(path == "/api/v1/clients" for _, path, _ in fake_api.calls)


@pytest.mark.asyncio
async def test_get_url_snapshot_timeline(fake_api) -> None:
    fake_api.json("GET", "/api/v1/archive/urls/u1/snapshots", _TIMELINE)
    result = await mcp.call_tool("get_url_snapshot_timeline", {"url_id": "u1"})
    assert not result.is_error
    snapshots = result.structured_content["snapshots"]
    assert [s["snapshot_id"] for s in snapshots] == ["s2", "s1"]
    assert snapshots[0]["has_change"] is True


@pytest.mark.asyncio
async def test_compare_url_versions_defaults_to_latest(fake_api) -> None:
    fake_api.json("GET", "/api/v1/archive/urls/u1/snapshots", _TIMELINE)
    fake_api.json("GET", "/api/v1/archive/snapshots/s2/diff", _DIFF)
    result = await mcp.call_tool("compare_url_versions", {"url_id": "u1"})
    assert not result.is_error
    body = result.structured_content
    assert body["from_date"] == "2026-08-01"
    assert body["to_date"] == "2026-09-01"
    assert body["diff_lines"] == ["-old text", "+new text"]


@pytest.mark.asyncio
async def test_compare_url_versions_on_the_oldest_snapshot_is_a_clear_error(fake_api) -> None:
    fake_api.json("GET", "/api/v1/archive/urls/u1/snapshots", _TIMELINE)
    with pytest.raises(ToolError, match="oldest one archived"):
        await mcp.call_tool("compare_url_versions", {"url_id": "u1", "target_date": "2026-08-01"})


@pytest.mark.asyncio
async def test_compare_url_versions_no_snapshots_at_all(fake_api) -> None:
    fake_api.json(
        "GET",
        "/api/v1/archive/urls/u1/snapshots",
        {"url": _TIMELINE["url"], "snapshots": []},
    )
    with pytest.raises(ToolError, match="no snapshots"):
        await mcp.call_tool("compare_url_versions", {"url_id": "u1"})


@pytest.mark.asyncio
async def test_list_archive_changes(fake_api) -> None:
    fake_api.json(
        "GET",
        "/api/v1/archive/changes",
        {
            "items": [
                {
                    "client_name": "Alpha Corp",
                    "url": "https://alpha.example.com/pricing",
                    "detected_at": "2026-09-01T00:00:00Z",
                    "change_kinds": ["content"],
                    "text_delta_words": 42,
                }
            ],
            "total": 1,
            "page": 1,
            "page_size": 50,
        },
    )
    result = await mcp.call_tool("list_archive_changes", {"days": 7})
    assert not result.is_error
    assert result.structured_content["total"] == 1
    # `days` reaches the API as a query param, not filtered client-side.
    _, _, params = fake_api.calls[-1]
    assert params["days"] == "7"
