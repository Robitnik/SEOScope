"""Резолвер «назва/домен → client_id» (план §7.1): людина каже "ALPHA",
не UUID. Кожен клієнтський tool резолвить це сам, перш ніж бити в API.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.mcp.apiclient import ApiClient
from app.mcp.errors import ApiError

# Досить з запасом для реального портфелю агенції; ширше вже не «список
# клієнтів», а сторінкування, якого тут свідомо нема (GET /clients саме
# так обмежує `limit`, дивись `app/api/deps.py::pagination`).
_MAX_CLIENTS = 500


@dataclass(frozen=True)
class ResolvedClient:
    client_id: str
    name: str
    domain: str


async def _all_clients(api: ApiClient) -> list[dict[str, Any]]:
    result = await api.get("/api/v1/clients", params={"limit": _MAX_CLIENTS})
    return result if isinstance(result, list) else []


def _identity_fields(client: dict[str, Any]) -> set[str]:
    return {
        client.get("name", "").casefold(),
        client.get("domain", "").casefold(),
        client.get("slug", "").casefold(),
    }


def _matches(client: dict[str, Any], needle: str) -> bool:
    needle = needle.casefold()
    return any(needle in field for field in _identity_fields(client))


async def resolve_client(api: ApiClient, name_or_domain: str) -> ResolvedClient:
    """Знайти рівно одного клієнта за (частиною) назви, домену чи slug-а.

    Спершу точний (без урахування регістру) збіг — типовий випадок, коли
    модель уже бачила повний список і повторює точну назву. Якщо точного
    збігу нема, підстрокове співпадіння: рівно один кандидат — приймається,
    інакше — помилка з переліком, з якого модель (чи людина через неї)
    може обрати правильний варіант, замість мовчазного "нічого не знайдено".
    """
    clients = await _all_clients(api)
    if not clients:
        raise ApiError("No clients are configured in this system yet.")

    needle = name_or_domain.strip()
    if not needle:
        raise ApiError("Client name or domain must not be empty.")

    exact = [c for c in clients if needle.casefold() in _identity_fields(c)]
    if len(exact) == 1:
        c = exact[0]
        return ResolvedClient(client_id=c["id"], name=c["name"], domain=c["domain"])

    candidates = exact if exact else [c for c in clients if _matches(c, needle)]
    if len(candidates) == 1:
        c = candidates[0]
        return ResolvedClient(client_id=c["id"], name=c["name"], domain=c["domain"])

    known = ", ".join(sorted(c["name"] for c in clients)[:20])
    more = "…" if len(clients) > 20 else ""
    if not candidates:
        raise ApiError(f'Client "{name_or_domain}" not found. Known clients: {known}{more}.')
    matched = ", ".join(f'{c["name"]} ({c["domain"]})' for c in candidates[:10])
    raise ApiError(
        f'"{name_or_domain}" matches multiple clients: {matched}. '
        "Use the exact name or domain to disambiguate."
    )
