"""Тест каталогу Tools для Character Counter (app/api/tools.py)."""

from __future__ import annotations

from typing import Any


def test_catalog_lists_char_counter(api_client: Any) -> None:
    response = api_client.get("/api/v1/tools")
    assert response.status_code == 200
    tools = {tool["key"]: tool for tool in response.json()["tools"]}
    assert "char-counter" in tools
    assert tools["char-counter"]["route"] == "#/tools/char-counter"
    # Інструмент нічого не зберігає в БД: `None`, а не підроблений `0` (PLAN §12).
    assert tools["char-counter"]["stat_label"] is None
    assert tools["char-counter"]["stat_value"] is None
