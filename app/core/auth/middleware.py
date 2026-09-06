"""Глобальний гейт (task-71 §6): закрито за замовчуванням.

Один middleware закриває ВСЕ одразу — REST, статику фронта, replay архіву,
вивантаження артефактів і звітів, — бо всі вони роздаються з одного застосунку.
Перевіряти доступ у роутерах було б рівнозначно обіцянці, що жоден із 164
наявних і всіх майбутніх ендпоінтів не забуде декоратор; тут забути нічого.

Порядок у стеку важливий. Starlette робить ОСТАННІЙ доданий middleware
зовнішнім, тому `configure_security` додає цей ПЕРШИМ: так 401/403 усе одно
проходять крізь `SecurityHeadersMiddleware` і CORS, а не летять «голими».
"""

from __future__ import annotations

import hmac

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.types import ASGIApp

from app.core.auth.policy import is_public_path, is_safe_method, required_role, role_satisfies
from app.core.auth.sessions import (
    AuthenticatedSession,
    resolve_agent_token,
    resolve_localhost_bypass,
    resolve_session,
)
from app.core.config import Settings, get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)

_CSRF_HEADER = "X-CSRF-Token"


def _error(status: int, code: str, message: str) -> JSONResponse:
    """Та сама форма `{detail: {error_code, message}}`, яку вже нормалізує
    `frontend/js/api.js` — фронту не треба вчити новий формат помилки."""
    return JSONResponse(
        status_code=status, content={"detail": {"error_code": code, "message": message}}
    )


def _wants_json(request: Request) -> bool:
    if request.url.path.startswith("/api/"):
        return True
    accept = request.headers.get("accept", "")
    return "application/json" in accept and "text/html" not in accept


class AuthMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: ASGIApp, settings: Settings) -> None:
        super().__init__(app)
        # Знімок на момент побудови застосунку — лише як фолбек. Реальні
        # налаштування читаються на кожен запит (`get_settings()` кешований,
        # тож це словниковий доступ): `app.main.app` створюється один раз на
        # процес, і без цього тест, який піднімає власну конфігурацію, отримав
        # би конфіг першого тесту в сесії.
        self._fallback_settings = settings

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        settings = self._settings()
        path = request.url.path

        if not settings.auth_enabled:
            request.state.auth_session = None
            return await call_next(request)

        # Preflight несе заголовків рівно стільки, скільки браузер вважає за
        # потрібне, і ніколи не несе cookie — відповідає CORS-шар вище.
        if request.method == "OPTIONS":
            return await call_next(request)

        # Два способи довести, хто робить запит, БЕЗ БД (task-73) —
        # обидва статичні, дешевші за cookie, і обидва мертві поза
        # `environment != "production"` незалежно від власного прапорця.
        # Перевіряються навіть на публічних шляхах: `GET /api/v1/auth/session`
        # публічний саме тому, що анонім теж має право спитати «чи я
        # залогінений», і відповідь на нього мусить бачити той-таки байпас,
        # яким живуть усі інші ендпоінти — інакше людина на localhost
        # проходить крізь гейт скрізь, а фронт на старті все одно веде її
        # на форму входу, бо сам питає `/auth/session` про cookie, якого нема.
        auth: AuthenticatedSession | None = resolve_agent_token(request, settings)
        if auth is None:
            auth = resolve_localhost_bypass(request, settings)

        if is_public_path(path):
            request.state.auth_session = auth
            return await call_next(request)

        if auth is None:
            token = request.cookies.get(settings.auth_cookie_name)
            if token:
                auth = await resolve_session(token, settings=settings)

        if auth is None:
            return self._unauthenticated(request)

        request.state.auth_session = auth

        if not role_satisfies(auth.role, required_role(request.method, path, settings)):
            logger.info(
                "access_denied user=%s role=%s method=%s path=%s",
                auth.email,
                auth.role.value,
                request.method,
                path,
            )
            return _error(
                403,
                "forbidden",
                "Your role does not allow this action. Ask an administrator for access.",
            )

        # CSRF стосується лише cookie: браузер ніколи не додає сам ні
        # `Authorization`, ні (за визначенням) з'єднання, зроблене НЕ його
        # власним запитом — тому й dev-токен, і localhost-байпас (task-73,
        # `origin != "cookie"`, `session_id is None`) структурно не можуть
        # бути підроблені чужим сайтом, і другий шар захисту тут зайвий.
        if not is_safe_method(request.method) and auth.session_id is not None:
            csrf_error = self._check_csrf(request, auth, settings)
            if csrf_error is not None:
                return csrf_error

        return await call_next(request)

    def _settings(self) -> Settings:
        try:
            return get_settings()
        except Exception:  # pragma: no cover - конфіг зламано; не мовчимо, але й не пускаємо
            return self._fallback_settings

    def _unauthenticated(self, request: Request) -> Response:
        if _wants_json(request):
            return _error(401, "auth_required", "Authentication required")
        # Навігація людини: ведемо на форму логіну і зберігаємо, куди вона
        # йшла — інакше після входу вона опиняється на дашборді замість
        # сторінки, посилання на яку їй прислали.
        target = request.url.path
        if request.url.query:
            target = f"{target}?{request.url.query}"
        return RedirectResponse(url=f"/login.html?next={_quote(target)}", status_code=303)

    def _check_csrf(
        self, request: Request, auth: AuthenticatedSession, settings: Settings
    ) -> Response | None:
        """Double-submit поверх `SameSite=Lax`.

        Cookie сам по собі вже не поїде на cross-site form POST, але другий,
        незалежний шар коштує один рядок і закриває випадки, де браузер/
        розширення послаблюють SameSite.
        """
        header = request.headers.get(_CSRF_HEADER, "")
        cookie = request.cookies.get(settings.auth_csrf_cookie_name, "")
        if not header or not hmac.compare_digest(header, auth.csrf_token):
            logger.info("csrf_failed user=%s path=%s", auth.email, request.url.path)
            return _error(403, "csrf_failed", "Missing or invalid CSRF token")
        if cookie and not hmac.compare_digest(cookie, auth.csrf_token):
            return _error(403, "csrf_failed", "Missing or invalid CSRF token")
        return None


def _quote(value: str) -> str:
    from urllib.parse import quote

    return quote(value, safe="")
