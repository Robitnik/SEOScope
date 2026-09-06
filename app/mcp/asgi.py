"""Підключення MCP-сервера й OAuth-маршрутів до FastAPI (task-78).

Чому маршрути ДОДАЮТЬСЯ поштучно, а не `app.mount("/", mcp_app)`: SDK
віддає готовий Starlette-застосунок, чиї шляхи (`/mcp`, `/authorize`,
`/token`, `/register`, `/revoke`, `/.well-known/**`) мають жити РІВНО в
корені домену — Claude шукає їх там і ніде більше. Змонтувати такий
застосунок у корінь неможливо: там уже стоїть статика фронта, і
`Mount("/")` проковтнув би геть усе. Тому беремо його `routes` і кладемо
у наш роутер перед статикою.

Lifespan SDK-застосунку при цьому НЕ виконується автоматично (його
запускає лише власний Starlette), тож `StreamableHTTPSessionManager.run()`
піднімається тут окремо — без нього перший же запит до `/mcp` впаде.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any
from urllib.parse import urlparse

from fastapi import FastAPI
from mcp.server.auth.middleware.auth_context import AuthContextMiddleware
from mcp.server.auth.middleware.bearer_auth import BearerAuthBackend
from mcp.server.transport_security import TransportSecuritySettings
from starlette.middleware.authentication import AuthenticationMiddleware

from app.core.config import Settings
from app.core.logging import get_logger

logger = get_logger(__name__)


def attach_mcp(app: FastAPI, settings: Settings) -> None:
    from app.mcp.server import mcp

    host = urlparse(settings.mcp_api_base_url).netloc or "127.0.0.1:6524"
    # Захист від DNS-rebinding у SDK за замовчуванням пускає лише
    # localhost — а до нас приходять із публічного домену через тунель,
    # тож обидва варіанти мають бути в списку явно.
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[host, "127.0.0.1:*", "localhost:*"],
        allowed_origins=[
            f"https://{host}",
            f"http://{host}",
            "http://127.0.0.1:*",
            "http://localhost:*",
        ],
    )

    def build() -> tuple[list[Any], Any]:
        """Свіжий транспорт + свій `StreamableHTTPSessionManager` до нього."""
        mcp_app = mcp.streamable_http_app(
            streamable_http_path="/mcp",
            transport_security=security,
            host=host,
        )
        # Bearer-автентифікацію SDK віддає НЕ в маршрутах, а окремим
        # списком middleware свого Starlette-застосунку — а ми переносимо
        # лише маршрути. Без цього `/mcp` бачить кожен запит як анонімний
        # і чесно відповідає 401 навіть на бездоганний токен (знайдено
        # вживу: `load_access_token` у процесі повертав валідний токен, а
        # той самий токен по HTTP отримував 401). Тому загортаємо РІВНО
        # цей маршрут — решті (`/authorize`, `/token`, `/register`,
        # `/.well-known/**`) автентифікація не потрібна за визначенням.
        verifier = mcp._token_verifier
        for route in mcp_app.routes:
            if getattr(route, "path", None) == "/mcp" and verifier is not None:
                inner = route.app  # type: ignore[attr-defined]
                route.app = AuthenticationMiddleware(  # type: ignore[attr-defined]
                    AuthContextMiddleware(inner), backend=BearerAuthBackend(verifier)
                )
        return list(mcp_app.routes), mcp.session_manager

    routes, _manager = build()
    app.router.routes.extend(routes)
    mcp_route = next(r for r in routes if getattr(r, "path", None) == "/mcp")

    previous_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(scope_app: FastAPI) -> AsyncIterator[None]:
        # Транспорт перебудовується НА КОЖЕН старт, і це не марнотратство:
        # `StreamableHTTPSessionManager.run()` дозволено викликати рівно
        # один раз на екземпляр, а `mcp` — модульний синглтон. У проді
        # застосунок стартує один раз і різниці немає; у тестах той самий
        # `app.main.app` піднімається десятками `TestClient`, і без цього
        # другий же підйом падає з «`.run()` can only be called once per
        # instance». Свіжий менеджер на старт робить обидва світи
        # однаковими.
        fresh_routes, manager = build()
        fresh = next(r for r in fresh_routes if getattr(r, "path", None) == "/mcp")
        mcp_route.app = fresh.app
        async with AsyncExitStack() as stack:
            await stack.enter_async_context(manager.run())
            logger.info("mcp_http_ready path=/mcp host=%s", host)
            await stack.enter_async_context(previous_lifespan(scope_app))
            yield

    app.router.lifespan_context = lifespan
