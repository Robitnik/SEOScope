"""Один шлях перетворення HTTP-помилки нашого API на текст, який читає модель.

Проєктний план (§7.3) розрізняє два класи помилок tools у MCP:

- **Protocol errors** — зіпсований запит/невідомий tool. Про них дбає сам
  SDK (`mcp.server.mcpserver.exceptions.ToolError`, яку піднімає SDK для
  невідомої назви tool-а чи вхідних даних, що не пройшли `inputSchema`) —
  цей модуль їх не породжує.
- **Tool execution errors** — щось не так із самим викликом (клієнта не
  знайдено, роль не дозволяє дію, ран не існує). Вони йдуть як
  `isError: true` з текстом-**дією**, не стектрейсом: модель має зрозуміти,
  що зробити інакше, а не побачити внутрішню структуру нашого API.

`ApiError` — підклас `ToolError`: SDK ловить `ToolError` і показує
`content`/`is_error=True` моделі, логуючи на INFO без трейсу — жодна інша
поведінка тут не потрібна, тому tools піднімають `ApiError` напряму.
"""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

# Заголовки, за якими 401 від AuthMiddleware і 403 від нашого API мають
# однаковий сенс для агента: "цей токен не годиться для цієї дії", а не
# "спробуй ще раз з тими самими аргументами".
_ROLE_HINT = (
    "This MCP token's role does not allow this action. "
    "Ask an admin for a token with a higher role if this action is expected to work."
)
_AUTH_HINT = "The MCP token was rejected (missing, invalid, or revoked). Check MCP_TOKEN."


class ApiError(ToolError):
    """Наша REST-помилка, перетворена на дію для моделі."""


def _detail_from_body(body: Any) -> dict[str, Any] | None:
    """Наша єдина форма помилки — `{"detail": {"error_code", "message"}}`.

    Той самий конверт віддають і `AuthMiddleware._error()` (401/403), і
    домейн-хендлери через `HTTPException(status, ErrorDTO(...).model_dump())`
    (404/409/422) — тому один розбір покриває обидва джерела.
    """
    if not isinstance(body, dict):
        return None
    detail = body.get("detail")
    if isinstance(detail, dict) and "message" in detail:
        return detail
    return None


def api_error(status_code: int, body: Any, *, method: str, path: str) -> ApiError:
    """Побудувати `ApiError` з відповіді нашого API (статус + JSON-тіло)."""
    detail = _detail_from_body(body)

    if status_code == 401:
        return ApiError(_AUTH_HINT)
    if status_code == 403:
        message = detail["message"] if detail else _ROLE_HINT
        return ApiError(f"{_ROLE_HINT} ({message})" if detail else _ROLE_HINT)
    if detail is not None:
        # 404/409/422 і подібні — наше власне повідомлення вже написане
        # як дія ("Client not found", "Invalid report parameters", ...).
        return ApiError(detail["message"])

    # Щось несподіване (5xx без нашого конверта, проксі, і т.д.) — не
    # передаємо тіло відповіді як є (може містити внутрішні деталі), лише
    # факт і координати виклику.
    return ApiError(f"Request to {method} {path} failed with HTTP {status_code}.")
