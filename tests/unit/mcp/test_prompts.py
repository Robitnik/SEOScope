"""Prompts (план §8): рецепт, який називає правильні tools у правильному
порядку — тест ловить typo в назві tool-а всередині тексту-інструкції,
яку інакше ніхто не перевіряє до живого запуску."""

from __future__ import annotations

import pytest

from app.mcp.server import mcp


async def _tool_names() -> set[str]:
    return {t.name for t in await mcp.list_tools()}


@pytest.mark.asyncio
async def test_page_history_answer_only_names_real_tools() -> None:
    result = await mcp.get_prompt("page_history_answer", {"url": "https://example.com/x"})
    text = result.messages[0].content.text
    for name in ("find_archived_url", "get_url_snapshot_timeline", "compare_url_versions"):
        assert f"{name}(" in text
        assert name in await _tool_names()


@pytest.mark.asyncio
async def test_weekly_change_digest_only_names_real_tools() -> None:
    result = await mcp.get_prompt("weekly_change_digest", {"client": "Alpha Corp"})
    text = result.messages[0].content.text
    for name in ("list_site_changes", "list_archive_changes"):
        assert f"{name}(" in text
        assert name in await _tool_names()
    assert "Alpha Corp" in text
