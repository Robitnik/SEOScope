"""`GET /api/v1/mcp/catalog` (task-77): каталог MCP-тулів для сторінки
`#/mcp`. Головний гейт тут — анти-розсинхрон: сторінка не сміє показувати
щось інше, ніж те, що сервер справді зареєстрував (план §2, §8)."""

from __future__ import annotations

import asyncio

import pytest

from app.mcp.server import mcp


def _tool_names() -> set[str]:
    return {t.name for t in asyncio.run(mcp.list_tools())}


def test_catalog_matches_the_live_mcp_server_exactly(client):
    response = client.get("/api/v1/mcp/catalog")
    assert response.status_code == 200
    body = response.json()

    assert {t["name"] for t in body["tools"]} == _tool_names()
    assert len(body["tools"]) == 12


def test_every_tool_has_exactly_one_group(client):
    body = client.get("/api/v1/mcp/catalog").json()

    for tool in body["tools"]:
        assert tool["group"] in body["groups"], tool["name"]
    # `groups` itself must not carry an entry nothing maps to — dead nav.
    used = {t["group"] for t in body["tools"]}
    assert used == set(body["groups"])


def test_tool_metadata_is_complete(client):
    body = client.get("/api/v1/mcp/catalog").json()

    write_tools = [t for t in body["tools"] if not t["read_only"]]
    assert {t["name"] for t in write_tools} == {"run_client_report"}

    for tool in body["tools"]:
        assert tool["title"], tool["name"]
        assert tool["description"], tool["name"]
        for arg in tool["args"]:
            assert arg["type"], f"{tool['name']}.{arg['name']}"
            assert arg["description"], f"{tool['name']}.{arg['name']} has no description"


def test_summary_is_first_paragraph_even_for_a_tool_with_no_args(client):
    # `list_clients` takes no arguments, so its docstring has no `Args:`
    # block at all — a distinct code path from every other tool, and one
    # that used to skip the "first paragraph only" trim entirely.
    body = client.get("/api/v1/mcp/catalog").json()
    list_clients = next(t for t in body["tools"] if t["name"] == "list_clients")

    assert list_clients["summary"] == (
        "List every client in the system: id, name, domain, and status."
    )
    assert "\n" not in list_clients["summary"]


def test_prompts_render_with_placeholders_matching_their_declared_args(client):
    body = client.get("/api/v1/mcp/catalog").json()

    names = {p["name"] for p in body["prompts"]}
    assert names == {"page_history_answer", "weekly_change_digest"}

    for prompt in body["prompts"]:
        assert prompt["rendered_text"]
        for arg in prompt["args"]:
            assert f"{{{{{arg['name']}}}}}" in prompt["rendered_text"], (
                f"{prompt['name']} doesn't mention its own {arg['name']} placeholder"
            )


def test_all_tools_prompt_mentions_every_tool_by_name(client):
    body = client.get("/api/v1/mcp/catalog").json()

    prompt = body["all_tools_prompt"]
    assert prompt
    for tool in body["tools"]:
        assert tool["name"] in prompt, f"{tool['name']} missing from the all-tools briefing"
    # The one write tool is called out, not just listed identically to the rest.
    assert "writes" in prompt.lower()


def test_resources_include_the_metrics_catalog(client):
    body = client.get("/api/v1/mcp/catalog").json()

    assert [r["uri"] for r in body["resources"]] == ["catalog://metrics"]
    assert body["resources"][0]["mime_type"] == "application/json"


def test_no_secret_values_leak_into_the_response(client, monkeypatch):
    monkeypatch.setenv("MCP_TOKEN", "super-secret-mcp-token")
    monkeypatch.setenv("AGENT_DEV_TOKEN", "super-secret-agent-token")
    from app.core.config import get_settings

    get_settings.cache_clear()
    try:
        response = client.get("/api/v1/mcp/catalog")
        assert "super-secret-mcp-token" not in response.text
        assert "super-secret-agent-token" not in response.text
    finally:
        get_settings.cache_clear()


def test_connect_snippets_never_embed_a_real_token(client):
    body = client.get("/api/v1/mcp/catalog").json()

    for snippet in body["connect"]:
        assert "MCP_TOKEN=" not in snippet["config"] or "<your-token>" in snippet["config"]


def test_starter_prompts_are_present_and_non_empty(client):
    body = client.get("/api/v1/mcp/catalog").json()

    assert len(body["starter_prompts"]) >= 3
    for prompt in body["starter_prompts"]:
        assert prompt["text"]
        assert prompt["scenario"]


def test_importing_mcp_server_has_no_side_effect_of_opening_a_client():
    """Task-77 §2: pulling `app.mcp.server` into the API process must not
    open an HTTP client — that only happens lazily when a MODEL calls a
    tool, never when a person opens this page."""
    from app.mcp import apiclient

    apiclient.reset_api_client_for_tests(client=None)
    import importlib

    importlib.reload(apiclient)
    assert apiclient._client is None


def test_unauthenticated_request_is_rejected(auth_client):
    response = auth_client.get("/api/v1/mcp/catalog")
    assert response.status_code == 401


def test_viewer_role_can_read_the_catalog(auth_client, make_user, sign_in):
    email, password = make_user("viewer@example.com", role="viewer")
    headers = sign_in(email, password)

    response = auth_client.get("/api/v1/mcp/catalog", headers=headers)
    assert response.status_code == 200
    assert len(response.json()["tools"]) == 12


@pytest.mark.parametrize("bad_name", ["not_a_real_tool"])
def test_unmapped_tool_would_fail_loudly_not_disappear_silently(bad_name, monkeypatch):
    """Regression guard for the exhaustiveness invariant itself (§3 of the
    plan): poke a fake entry out of `_TOOL_GROUPS` and confirm the builder
    raises instead of quietly rendering the page short one card."""
    from app.api import mcp_catalog

    monkeypatch.setattr(mcp_catalog, "_TOOL_GROUPS", {})
    with pytest.raises(RuntimeError, match="has no entry in _TOOL_GROUPS"):
        asyncio.run(mcp_catalog._build_tools())
