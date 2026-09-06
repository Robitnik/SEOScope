"""Група H — get_job_status: один tool, чотири kind (план §6)."""

from __future__ import annotations

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from app.mcp.server import mcp

_CLIENTS = [{"id": "c1", "name": "Alpha Corp", "domain": "alpha.example.com", "slug": "alpha"}]


@pytest.mark.asyncio
async def test_report_run_done_points_to_get_report_data(fake_api) -> None:
    fake_api.json(
        "GET",
        "/api/v1/reports/runs/run1",
        {
            "id": "run1",
            "client_id": "c1",
            "kind": "monthly",
            "source": "api",
            "status": "succeeded",
            "period_count": 4,
            "periods_done": 4,
            "periods_failed": 0,
            "cross_check_status": None,
            "error_code": None,
            "error_message": None,
            "cancel_requested": False,
        },
    )
    result = await mcp.call_tool("get_job_status", {"job_id": "run1", "kind": "report_run"})
    assert not result.is_error
    body = result.structured_content
    assert body["done"] is True
    assert body["progress_percent"] == 100.0
    assert body["read_result_with"] == "get_report_data"
    assert body["error_message"] is None


@pytest.mark.asyncio
async def test_report_run_still_running_has_no_read_result_with(fake_api) -> None:
    fake_api.json(
        "GET",
        "/api/v1/reports/runs/run1",
        {
            "id": "run1",
            "client_id": "c1",
            "kind": "monthly",
            "source": "api",
            "status": "running",
            "period_count": 4,
            "periods_done": 2,
            "periods_failed": 0,
            "cross_check_status": None,
            "error_code": None,
            "error_message": None,
            "cancel_requested": False,
        },
    )
    result = await mcp.call_tool("get_job_status", {"job_id": "run1", "kind": "report_run"})
    body = result.structured_content
    assert body["done"] is False
    assert body["progress_percent"] == 50.0
    assert body["read_result_with"] is None


@pytest.mark.asyncio
async def test_failed_report_run_surfaces_its_error_message(fake_api) -> None:
    fake_api.json(
        "GET",
        "/api/v1/reports/runs/run1",
        {
            "id": "run1",
            "client_id": "c1",
            "kind": "monthly",
            "source": "api",
            "status": "failed",
            "period_count": 4,
            "periods_done": 1,
            "periods_failed": 3,
            "cross_check_status": None,
            "error_code": "provider_error",
            "error_message": "Reporting Ninja returned 500 for all periods.",
            "cancel_requested": False,
        },
    )
    result = await mcp.call_tool("get_job_status", {"job_id": "run1", "kind": "report_run"})
    body = result.structured_content
    assert body["done"] is True
    assert body["error_message"] == "Reporting Ninja returned 500 for all periods."
    assert body["read_result_with"] is None


@pytest.mark.asyncio
async def test_sync_job_status(fake_api) -> None:
    fake_api.json(
        "GET",
        "/api/v1/sync-jobs/job1",
        {
            "id": "job1",
            "batch_id": None,
            "client_id": "c1",
            "period_kind": "week",
            "comparison_mode": "closed",
            "anchor_date": "2026-09-06",
            "requested_providers": ["gsc"],
            "status": "running",
            "progress_percent": 40.0,
            "current_stage": "collecting",
            "attempt": 1,
            "created_at": "2026-09-06T00:00:00Z",
        },
    )
    result = await mcp.call_tool("get_job_status", {"job_id": "job1", "kind": "sync_job"})
    body = result.structured_content
    assert body["status"] == "running"
    assert body["done"] is False
    assert body["progress_percent"] == 40.0


@pytest.mark.asyncio
async def test_archive_capture_progress_from_urls_captured(fake_api) -> None:
    fake_api.json(
        "GET",
        "/api/v1/archive/captures/cap1",
        {
            "id": "cap1",
            "client_id": "c1",
            "client_name": "Alpha Corp",
            "slot_key": "daily",
            "scheduled_for": "2026-09-06T00:00:00Z",
            "trigger": "manual",
            "status": "running",
            "started_at": "2026-09-06T00:00:00Z",
            "finished_at": None,
            "urls_total": 10,
            "urls_captured": 4,
            "urls_unchanged": 0,
            "urls_new": 0,
            "urls_failed": 0,
            "urls_blocked": 0,
            "assets_fetched": 0,
            "assets_reused": 0,
            "assets_failed": 0,
            "bytes_stored": 0,
            "error_code": None,
            "error_message": None,
            "scope": "client",
            "target_url": None,
            "progress_message": None,
        },
    )
    result = await mcp.call_tool(
        "get_job_status", {"job_id": "cap1", "kind": "archive_capture"}
    )
    body = result.structured_content
    assert body["progress_percent"] == 40.0
    assert body["done"] is False


@pytest.mark.asyncio
async def test_monitor_run_requires_client(fake_api) -> None:
    with pytest.raises(ToolError, match="also needs `client`"):
        await mcp.call_tool("get_job_status", {"job_id": "run1", "kind": "monitor_run"})


@pytest.mark.asyncio
async def test_monitor_run_looks_up_by_id_in_the_clients_run_list(fake_api) -> None:
    fake_api.json("GET", "/api/v1/clients", _CLIENTS)
    fake_api.json(
        "GET",
        "/api/v1/clients/c1/monitor/runs",
        [
            {
                "id": "run1",
                "client_id": "c1",
                "kind": "light",
                "trigger": "manual",
                "status": "succeeded",
                "started_at": "2026-09-06T00:00:00Z",
                "finished_at": "2026-09-06T00:05:00Z",
                "urls_total": 20,
                "urls_checked": 20,
                "urls_changed": 1,
                "urls_new": 0,
                "urls_gone": 0,
                "requests_made": 20,
                "bytes_downloaded": 1000,
                "changes_critical": 0,
                "changes_warning": 1,
                "changes_info": 0,
                "truncated": False,
                "remaining_count": None,
                "error_code": None,
                "error_message": None,
                "blocked_count": None,
            }
        ],
    )
    result = await mcp.call_tool(
        "get_job_status", {"job_id": "run1", "kind": "monitor_run", "client": "Alpha"}
    )
    body = result.structured_content
    assert body["done"] is True
    assert body["progress_percent"] == 100.0


@pytest.mark.asyncio
async def test_monitor_run_unknown_id_is_a_tool_error(fake_api) -> None:
    fake_api.json("GET", "/api/v1/clients", _CLIENTS)
    fake_api.json("GET", "/api/v1/clients/c1/monitor/runs", [])
    with pytest.raises(ToolError, match="No monitor run"):
        await mcp.call_tool(
            "get_job_status", {"job_id": "ghost", "kind": "monitor_run", "client": "Alpha"}
        )
