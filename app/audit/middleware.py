"""Автоматичне зняття HTTP-дій (task-72).

Знімає рівно те, що є ДІЄЮ:
- будь-який `POST/PUT/PATCH/DELETE` — успішний і невдалий однаково;
- кожен 401/403 (`outcome="denied"`) — спроби доступу видно так само, як самі
  дії, і саме вони найцікавіші через місяць;
- перегляд адмінських екранів і експорти — це теж дія (винесення даних).

Звичайні GET-и дашборда НЕ журналюються: на один рендер їх десятки, і
таблиця перетворилась би на лог трафіку, у якому справжню дію не знайти.

Тіло мутуючого запиту зберігається редагованим і обрізаним
(`app/audit/redaction.py`). Секрети сюди не потрапляють — це сильніший
інваріант, ніж повнота журналу.
"""

from __future__ import annotations

import json
import time
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Match
from starlette.types import ASGIApp

from app.audit.actions import EXPORT_PREFIXES, ActionSpec, fallback_action, lookup
from app.audit.sink import AuditRecord, get_audit_sink
from app.core.config import Settings, get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)

_MUTATING = frozenset({"POST", "PUT", "PATCH", "DELETE"})
# З тіла логіну беремо ЛИШЕ email: «хто намагався зайти» — головна цінність
# журналу входів, а пароль не має проходити через цей код узагалі, навіть
# щоб бути заміненим на маску.
_LOGIN_PATH = "/api/v1/auth/login"
_MAX_BODY_BYTES = 64 * 1024


class AuditMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: ASGIApp, settings: Settings) -> None:
        super().__init__(app)
        self._fallback_settings = settings

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        settings = self._settings()
        if not settings.audit_enabled or request.method == "OPTIONS":
            return await call_next(request)

        method = request.method.upper()
        path = request.url.path
        interesting = method in _MUTATING or self._is_audited_read(request, method, path)

        body: dict[str, Any] | None = None
        if interesting and method in _MUTATING:
            body = await self._read_body(request)
            if path == _LOGIN_PATH:
                body = {"email": (body or {}).get("email")}

        started = time.perf_counter()
        response = await call_next(request)
        duration_ms = int((time.perf_counter() - started) * 1000)

        # 403 журналюємо завжди. 401 — лише на мутаціях і на логіні: інакше
        # одна вкладка з протухлою сесією створювала б десятки однакових
        # рядків за один рендер дашборда, і справжню відмову в них не знайти.
        denied = response.status_code == 403 or (
            response.status_code == 401 and (method in _MUTATING or path == _LOGIN_PATH)
        )
        if not interesting and not denied:
            return response

        try:
            self._record(request, response, method, path, body, duration_ms, denied)
        except Exception as exc:  # noqa: BLE001
            # Журнал ніколи не ламає відповідь, яку він описує.
            logger.warning("audit_capture_failed path=%s error=%s", path, exc)
        return response

    # ── внутрішнє ────────────────────────────────────────────────────────

    def _settings(self) -> Settings:
        try:
            return get_settings()
        except Exception:  # pragma: no cover
            return self._fallback_settings

    def _is_audited_read(self, request: Request, method: str, path: str) -> bool:
        if method != "GET":
            return False
        if path.startswith("/api/v1/admin/"):
            return True
        template = self._template(request) or path
        return any(template.startswith(prefix) for prefix in EXPORT_PREFIXES)

    async def _read_body(self, request: Request) -> dict[str, Any] | None:
        """Читає тіло так, щоб хендлер потім прочитав його ще раз.

        `Request.body()` кешує прочитане в `request._body`, і подальший
        `await request.json()` у роутері бере цей самий кеш — без цього
        журнал «з'їдав» би тіло й кожен POST падав би на порожньому запиті.
        """
        content_type = request.headers.get("content-type", "")
        if "application/json" not in content_type:
            return None
        try:
            raw = await request.body()
        except Exception:  # noqa: BLE001
            return None
        if not raw or len(raw) > _MAX_BODY_BYTES:
            return {"body_bytes": len(raw)} if raw else None
        try:
            parsed = json.loads(raw)
        except ValueError:
            return None
        return parsed if isinstance(parsed, dict) else {"body": parsed}

    def _template(self, request: Request) -> str | None:
        """Шаблон маршруту (`/api/v1/clients/{client_id}`), а не конкретний шлях.

        Саме він — ключ мапи дій, і саме він робить журнал агрегованим: інакше
        кожен клієнт створював би власну «дію».
        """
        route = request.scope.get("route")
        if route is not None and getattr(route, "path", None):
            return str(route.path)
        # Запит, зупинений на авторизації, до маршрутизації не дійшов — але
        # шлях у нього вже є, і саме він цікавий у «denied».
        for candidate in request.app.routes:
            try:
                match, _ = candidate.matches(request.scope)
            except Exception:  # noqa: BLE001
                continue
            if match is not Match.NONE and getattr(candidate, "path", None):
                return str(candidate.path)
        return None

    def _record(
        self,
        request: Request,
        response: Response,
        method: str,
        path: str,
        body: dict[str, Any] | None,
        duration_ms: int,
        denied: bool,
    ) -> None:
        template = self._template(request) or path
        spec: ActionSpec | None = lookup(method, template)
        action = spec.action if spec else fallback_action(method, template)

        if denied:
            # Заборонену спробу називаємо тим, що людина намагалась зробити,
            # а не «forbidden» — інакше в журналі не видно наміру.
            outcome = "denied"
        elif response.status_code >= 400:
            outcome = "failure"
        else:
            outcome = "success"

        auth = getattr(request.state, "auth_session", None)
        path_params: dict[str, Any] = request.scope.get("path_params") or {}
        resource_id = None
        if spec and spec.resource_param:
            resource_id = path_params.get(spec.resource_param)
        elif "client_id" in path_params:
            resource_id = path_params["client_id"]

        details: dict[str, Any] = {}
        if body:
            details["request"] = body
        if request.url.query:
            details["query"] = request.url.query[:500]

        record = AuditRecord(
            action=action,
            actor_kind="user" if auth else "anonymous",
            actor_id=getattr(auth, "user_id", None),
            actor_email=getattr(auth, "email", None),
            actor_name=getattr(auth, "name", None),
            actor_role=getattr(getattr(auth, "role", None), "value", None),
            session_id=getattr(auth, "session_id", None),
            ip=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
            resource_type=spec.resource_type if spec else None,
            resource_id=str(resource_id) if resource_id is not None else None,
            method=method,
            path=path,
            status_code=response.status_code,
            outcome=outcome,
            duration_ms=duration_ms,
            details=details,
        )
        get_audit_sink().submit(record)
