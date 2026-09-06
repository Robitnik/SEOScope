"""Резолвер «назва/домен → client_id» (план §7.1)."""

from __future__ import annotations

import pytest

from app.mcp.apiclient import get_api_client
from app.mcp.errors import ApiError
from app.mcp.resolve import resolve_client

_CLIENTS = [
    {"id": "c1", "name": "Alpha Corp", "domain": "alpha.example.com", "slug": "alphacorp"},
    {"id": "c2", "name": "Alpha Beta Studio", "domain": "alphabeta.example.com", "slug": "ab"},
]


@pytest.mark.asyncio
async def test_exact_name_match_wins_over_ambiguous_substrings(fake_api) -> None:
    fake_api.json("GET", "/api/v1/clients", _CLIENTS)
    resolved = await resolve_client(get_api_client(), "Alpha Corp")
    assert resolved.client_id == "c1"


@pytest.mark.asyncio
async def test_domain_match(fake_api) -> None:
    fake_api.json("GET", "/api/v1/clients", _CLIENTS)
    resolved = await resolve_client(get_api_client(), "alphabeta.example.com")
    assert resolved.client_id == "c2"


@pytest.mark.asyncio
async def test_unknown_client_lists_known_names(fake_api) -> None:
    fake_api.json("GET", "/api/v1/clients", _CLIENTS)
    with pytest.raises(ApiError, match="not found"):
        await resolve_client(get_api_client(), "Gamma Inc")


@pytest.mark.asyncio
async def test_ambiguous_substring_lists_candidates(fake_api) -> None:
    fake_api.json("GET", "/api/v1/clients", _CLIENTS)
    with pytest.raises(ApiError, match="multiple clients"):
        await resolve_client(get_api_client(), "Alpha")


@pytest.mark.asyncio
async def test_empty_client_list_gives_a_plain_message(fake_api) -> None:
    fake_api.json("GET", "/api/v1/clients", [])
    with pytest.raises(ApiError, match="No clients are configured"):
        await resolve_client(get_api_client(), "anything")
