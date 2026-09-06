"""Група A — Орієнтація (план §5): «хто в нас є і як загалом».

`list_clients` — фундамент: без нього резолвер (`app/mcp/resolve.py`) не
має що резолвити. `get_client_overview` — типова перша відповідь на
«розкажи про клієнта».

`get_portfolio_overview` — у каталозі плану (§5 група A), але поза
переліком 12 tools хвилі 1 (план §11, DoD п.1) — свідомо не тут, щоб
хвиля 1 лишалась рівно тим обсягом, який заплановано.
"""

from __future__ import annotations

from typing import Any, cast

from app.mcp.apiclient import ApiClient, get_api_client
from app.mcp.errors import ApiError
from app.mcp.models import ClientOverview, ClientSummary, ProviderStatusSummary
from app.mcp.resolve import resolve_client
from app.mcp.server import mcp


@mcp.tool()
async def list_clients() -> list[ClientSummary]:
    """List every client in the system: id, name, domain, and status.

    Call this first when you don't already know a client's exact name or
    domain — every other client-scoped tool resolves its `client` argument
    against this same list, so a mismatch here means a mismatch everywhere.
    """
    api = get_api_client()
    rows = await api.get("/api/v1/clients", params={"limit": 500})
    return [
        ClientSummary(
            client_id=row["id"],
            name=row["name"],
            domain=row["domain"],
            status=row["status"],
            is_dev=row.get("is_dev", False),
        )
        for row in rows
    ]


def _provider_summaries(card: dict[str, Any]) -> list[ProviderStatusSummary]:
    return [
        ProviderStatusSummary(
            provider=p["provider"],
            status=p["state"],
            last_synced_at=p.get("last_success_at"),
        )
        for p in card.get("providers", [])
    ]


def _metric_summary(card: dict[str, Any]) -> dict[str, Any]:
    """Health score plus every metric card, keyed by `metric_key`.

    Kept close to what the API already computed — `better_direction` and
    `is_improvement` travel with each metric here (unlike the raw report
    table, see `catalog://metrics` and `get_report_data`), so no extra
    lookup is needed to say whether a change is good or bad.
    """
    health = card.get("health", {})
    contributing = health.get("coverage_contributing", 0)
    configured = health.get("coverage_configured", 0)
    out: dict[str, Any] = {
        "health_score": health.get("value"),
        "health_coverage": f"{contributing}/{configured}",
    }
    for provider in card.get("providers", []):
        for metric in provider.get("metrics", []):
            out[metric["metric_key"]] = {
                "current": metric.get("current_text") or metric.get("current_value"),
                "delta_pct": metric.get("delta_pct"),
                "is_improvement": metric.get("is_improvement"),
            }
    return out


async def _find_client_card(api: ApiClient, client_id: str, name: str) -> dict[str, Any]:
    result = await api.get("/api/v1/dashboard", params={"q": name, "client_status": "all"})
    for card in result.get("clients", []):
        if card.get("client_id") == client_id:
            # `api.get()` returns `Any` (raw JSON) — the shape is only a
            # convention with the REST layer, not something mypy can verify.
            return cast(dict[str, Any], card)
    # The name that resolved to this client_id no longer matches it in the
    # dashboard's own search — should not happen outside a renamed-client
    # race, but fail loudly rather than silently return someone else's card.
    raise ApiError(f'Resolved client "{name}" but the dashboard has no matching card for it.')


@mcp.tool()
async def get_client_overview(client: str) -> ClientOverview:
    """Get a one-call summary of a client: status, health, and every
    configured provider's key metrics with deltas.

    Args:
        client: The client's name or domain (e.g. "Alpha Corp" or
            "alpha.example.com"). Use `list_clients` first if unsure of
            the exact spelling.
    """
    api = get_api_client()
    resolved = await resolve_client(api, client)
    card = await _find_client_card(api, resolved.client_id, resolved.name)
    return ClientOverview(
        client_id=resolved.client_id,
        name=resolved.name,
        domain=resolved.domain,
        status=card.get("status", "active"),
        overall_status=card.get("overall_status", "ok"),
        last_synced_at=card.get("last_synced_at"),
        providers=_provider_summaries(card),
        metrics=_metric_summary(card),
    )
