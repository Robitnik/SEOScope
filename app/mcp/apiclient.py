"""Тонкий async-клієнт до НАШОГО REST API (план §2 — єдиний шлях до даних).

Свідомо на `httpx2`, не на `httpx`: `httpx2` — головна (main-group)
залежність (її притягнув `mcp`, див. task-74 план §3.2), гарантовано
присутня в кожному образі; `httpx` живе лише в dev-групі
(`pyproject.toml`) для `fastapi.testclient.TestClient` і не є контрактом,
на який має спиратися рантайм-код.

Клієнт не імпортує `app.db`/`app.jobs`/`app.reports`/`app.monitor` — лише
`app.core.config` (налаштування — не доменна логіка) і `httpx2`.
"""

from __future__ import annotations

from typing import Any

import httpx2

from app.core.config import Settings, get_settings
from app.mcp.errors import api_error

_DEFAULT_TIMEOUT = 30.0


class ApiClient:
    """Ходить у `{base_url}/api/v1/**` з тим самим bearer-токеном, яким
    себе автентифікує агент/оператор (task-73's `resolve_agent_token`, і
    майбутній `agent_tokens` у хвилі 2 — цей клієнт не знає, який саме
    механізм перевіряє токен на боці сервера, лише що заголовок той самий).
    """

    def __init__(
        self,
        *,
        base_url: str,
        token: str | None,
        timeout: float = _DEFAULT_TIMEOUT,
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        headers = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        # `transport` — тестовий гачок (`httpx2.MockTransport`); лишається
        # `None` у продовому шляху, де `httpx2.AsyncClient` сам відкриває
        # реальне з'єднання.
        self._http = httpx2.AsyncClient(
            base_url=base_url, headers=headers, timeout=timeout, transport=transport
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def get(self, path: str, *, params: dict[str, Any] | None = None) -> Any:
        return await self._request("GET", path, params=params)

    async def post(self, path: str, *, json: dict[str, Any] | None = None) -> Any:
        return await self._request("POST", path, json=json)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> Any:
        # `None`-значення в query — прибираємо, а не серіалізуємо як
        # рядок "None": FastAPI інакше прочитає їх як буквальний текст.
        clean_params = (
            {k: v for k, v in params.items() if v is not None} if params is not None else None
        )
        try:
            response = await self._http.request(method, path, params=clean_params, json=json)
        except httpx2.HTTPError as exc:
            raise api_error(0, None, method=method, path=path) from exc

        if response.status_code >= 400:
            body: Any = None
            try:
                body = response.json()
            except ValueError:
                pass
            raise api_error(response.status_code, body, method=method, path=path)

        if response.status_code == 204 or not response.content:
            return None
        return response.json()


_client: ApiClient | None = None


def _resolve_token(settings: Settings) -> str | None:
    # У хвилі 1 MCP автентифікується тим самим dev-only bearer-токеном, що
    # й агенти-розробники (task-73) — окреме поле `mcp_token` існує лише
    # для читабельності конфігурації; якщо його не задали, `agent_dev_token`
    # лишається робочим фолбеком для локального stdio-запуску. Обидва
    # структурно мертві на `environment == "production"` на боці сервера
    # (`resolve_agent_token`) — це очікувано для хвилі 1 (див. план §4.2).
    return settings.mcp_token or settings.agent_dev_token


def get_api_client() -> ApiClient:
    """Синглтон на процес — stdio-сервер живе одним довгим процесом, і
    новий `httpx2.AsyncClient` на кожен виклик tool-а лише плодив би
    з'єднання без жодної користі (нема сесій, які він міг би нести)."""
    global _client
    if _client is None:
        settings = get_settings()
        _client = ApiClient(base_url=settings.mcp_api_base_url, token=_resolve_token(settings))
    return _client


def reset_api_client_for_tests(client: ApiClient | None = None) -> None:
    """Тестовий гачок: підмінити синглтон фейковим клієнтом (або скинути
    на `None`, щоб наступний `get_api_client()` перечитав налаштування)."""
    global _client
    _client = client
