"""Реєстрація сервера (план §3.1, §11 DoD п.1): рівно хвиля 1.

12 tools, 2 prompts, 1 resource — не більше й не менше. Число тут не
довільне: воно в самому плані як контрольна точка хвилі 1, і тест не дає
йому непомітно розповзтися чи всохнути.
"""

from __future__ import annotations

import pytest

from app.mcp.server import mcp

_EXPECTED_TOOLS = {
    "list_clients",
    "get_client_overview",
    "find_archived_url",
    "get_url_snapshot_timeline",
    "compare_url_versions",
    "list_archive_changes",
    "list_site_changes",
    "diff_monitored_page",
    "get_client_monthly_performance",
    "get_report_data",
    "run_client_report",
    "get_job_status",
}


@pytest.mark.asyncio
async def test_wave_one_has_exactly_twelve_tools() -> None:
    names = {t.name for t in await mcp.list_tools()}
    assert names == _EXPECTED_TOOLS


@pytest.mark.asyncio
async def test_tools_list_order_is_deterministic() -> None:
    first = [t.name for t in await mcp.list_tools()]
    second = [t.name for t in await mcp.list_tools()]
    assert first == second


@pytest.mark.asyncio
async def test_every_tool_has_a_description_and_a_valid_input_schema() -> None:
    for tool in await mcp.list_tools():
        assert tool.description, f"{tool.name} has no description"
        schema = tool.input_schema
        assert schema is not None, f"{tool.name} has a null inputSchema"
        assert schema.get("type") == "object", f"{tool.name}'s inputSchema is not an object"


@pytest.mark.asyncio
async def test_wave_one_has_two_prompts_and_one_resource() -> None:
    prompts = {p.name for p in await mcp.list_prompts()}
    assert prompts == {"page_history_answer", "weekly_change_digest"}

    resources = [str(r.uri) for r in await mcp.list_resources()]
    assert resources == ["catalog://metrics"]
