"""OAuth-сервер для remote MCP-конектора (task-78).

Навіщо взагалі OAuth, якщо в нас уже є особисті токени (task-79): custom
connector на claude.ai не має поля «встав свій токен» для звичайної
людини — статичні заголовки (`static_headers`) там у беті й вводить їх
адміністратор ОРГАНІЗАЦІЇ, один на всіх. Єдиний спосіб, що працює з
коробки для окремого користувача, — `oauth_dcr`: Dynamic Client
Registration + authorization code + PKCE. Тому цей модуль.

Головне архітектурне рішення: **токен доступу, який видає цей потік, —
це звичайний рядок `agent_tokens`**. Не паралельна сутність із власним
терміном дії, власним відкликанням і власним місцем у журналі, а той
самий об'єкт, який людина створює собі в кабінеті. Наслідки, заради яких
це й зроблено:

* права рахуються тим самим `resolve_personal_token_sync` — тобто
  `min(роль токена, ПОТОЧНА роль власника)`, і пониження людини або
  `is_active=False` вбиває конектор негайно;
* журнал (task-72) бачить реальну людину за викликом, а не «конектор»;
* людина бачить підключення в `#/account` серед своїх токенів і
  відкликає його там же, без окремого екрана «мої застосунки».

Клієнти реєструються ЛИШЕ як public (`token_endpoint_auth_method="none"`)
— ми не видаємо client_secret взагалі. Це тримає інваріант проєкту
«секрети не потрапляють у БД» цілим, і не втрачає нічого: Claude і так
шле PKCE `S256` на кожному запиті авторизації, а специфікація MCP саме
його вважає захистом коду для public-клієнта.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from app.core.config import get_settings
from app.db.base import utcnow
from app.db.repositories import oauth as repo
from app.db.repositories.agent_tokens import AgentTokenRepository
from app.db.repositories.users import UserRepository
from app.db.session import get_async_sessionmaker
from app.schemas.enums import UserRole

# Скільки живе токен доступу. Коротко навмисно: Claude оновлює його по
# refresh автоматично й реактивно на 401, тож короткий строк нічого не
# коштує користувачу, але звужує вікно для вкраденого значення.
ACCESS_TOKEN_DAYS = 30

# Єдиний scope. Розсипати права по scope-ах було б дублюванням: справжня
# межа прав у цьому тулі — роль користувача, і вона вже перевіряється на
# кожному запиті до API.
SCOPE = "yovth:use"

_CONNECTOR_LABEL_PREFIX = "Claude connector"


class YovthOAuthProvider(
    OAuthAuthorizationServerProvider[AuthorizationCode, RefreshToken, AccessToken]
):
    """Реалізація протоколу SDK поверх наших таблиць.

    Кожен метод бере власну сесію БД: методи протоколу викликаються
    ізольовано, з різних HTTP-запитів (реєстрація, згода, обмін коду,
    оновлення), і спільної транзакції між ними не існує в принципі.
    """

    async def _run(self, fn: Any) -> Any:
        async with get_async_sessionmaker()() as db:
            result = await db.run_sync(fn)
            await db.commit()
            return result

    # --- реєстрація клієнта (RFC 7591) ---

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        payload = await self._run(lambda s: repo.load_client(s, client_id))
        if payload is None:
            return None
        return OAuthClientInformationFull.model_validate(payload)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        # Клієнт лишається public незалежно від того, що він попросив:
        # секрет ми не видаємо й не зберігаємо (див. докстрінг модуля).
        client_info.client_secret = None
        client_info.token_endpoint_auth_method = "none"
        payload = client_info.model_dump(mode="json", exclude_none=True)
        await self._run(lambda s: repo.save_client(s, client_info.client_id, payload))

    # --- крок згоди ---

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        """Не видає код одразу — веде людину на НАШ екран згоди.

        Повернений URL це сторінка тула, а не redirect_uri клієнта: код
        з'явиться лише після того, як людина увійде під собою й натисне
        Approve (`POST /api/v1/oauth/consent`).
        """
        if not params.code_challenge:
            raise AuthorizeError("invalid_request", "PKCE code_challenge is required")

        def _create(s: Any) -> str:
            row = repo.create_pending(
                s,
                client_id=client.client_id,
                scopes=params.scopes or [SCOPE],
                code_challenge=params.code_challenge,
                redirect_uri=str(params.redirect_uri),
                redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
                state=params.state,
                resource=params.resource,
            )
            return str(row.id)

        pending_id = await self._run(_create)
        base = get_settings().mcp_api_base_url.rstrip("/")
        return f"{base}/#/oauth/consent?{urlencode({'request': pending_id})}"

    # --- обмін коду ---

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        def _load(s: Any) -> AuthorizationCode | None:
            row = repo.get_auth_code(s, authorization_code)
            if row is None or row.client_id != client.client_id:
                return None
            return AuthorizationCode(
                code=authorization_code,
                scopes=row.scopes.split() or [SCOPE],
                expires_at=row.expires_at.timestamp(),
                client_id=row.client_id,
                code_challenge=row.code_challenge,
                redirect_uri=row.redirect_uri,  # type: ignore[arg-type]
                redirect_uri_provided_explicitly=row.redirect_uri_provided_explicitly,
                resource=row.resource,
                subject=row.user_id,
            )

        code: AuthorizationCode | None = await self._run(_load)
        return code

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        def _exchange(s: Any) -> OAuthToken:
            row = repo.get_auth_code(s, authorization_code.code)
            if row is None or row.client_id != client.client_id:
                raise TokenError("invalid_grant", "authorization code is not valid")
            # Одноразовість — до видачі токенів: якщо щось нижче впаде,
            # код усе одно вже не можна пред'явити вдруге.
            repo.consume_auth_code(s, row)
            access = _issue_access_token(
                s, user_id=row.user_id, client_id=row.client_id, client=client
            )
            refresh = repo.issue_refresh_token(
                s, client_id=row.client_id, user_id=row.user_id, scopes=row.scopes
            )
            return OAuthToken(
                access_token=access,
                token_type="Bearer",
                expires_in=ACCESS_TOKEN_DAYS * 24 * 3600,
                scope=row.scopes or SCOPE,
                refresh_token=refresh,
            )

        token: OAuthToken = await self._run(_exchange)
        return token

    # --- оновлення ---

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        def _load(s: Any) -> RefreshToken | None:
            row = repo.get_refresh_token(s, refresh_token)
            if row is None or row.client_id != client.client_id:
                return None
            return RefreshToken(
                token=refresh_token,
                client_id=row.client_id,
                scopes=row.scopes.split() or [SCOPE],
                expires_at=int(row.expires_at.timestamp()),
                subject=row.user_id,
            )

        refresh: RefreshToken | None = await self._run(_load)
        return refresh

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        def _exchange(s: Any) -> OAuthToken:
            row = repo.get_refresh_token(s, refresh_token.token)
            if row is None or row.client_id != client.client_id:
                raise TokenError("invalid_grant", "refresh token is not valid")
            # Ротація: старий refresh помирає в тій самій транзакції, що
            # народжує новий — цього вимагає MCP-спека для public-клієнтів.
            repo.revoke_refresh_token(s, row)
            access = _issue_access_token(
                s, user_id=row.user_id, client_id=row.client_id, client=client
            )
            fresh = repo.issue_refresh_token(
                s, client_id=row.client_id, user_id=row.user_id, scopes=row.scopes
            )
            return OAuthToken(
                access_token=access,
                token_type="Bearer",
                expires_in=ACCESS_TOKEN_DAYS * 24 * 3600,
                scope=row.scopes or SCOPE,
                refresh_token=fresh,
            )

        token: OAuthToken = await self._run(_exchange)
        return token

    # --- перевірка токена доступу ---

    async def load_access_token(self, token: str) -> AccessToken | None:
        def _load(s: Any) -> AccessToken | None:
            row = AgentTokenRepository(s).get_by_token(token)
            if row is None or row.revoked_at is not None:
                return None
            if row.expires_at <= utcnow():
                return None
            # Власник міг бути деактивований після видачі — тоді конектор
            # має померти негайно, як і будь-який інший його токен.
            user = UserRepository(s).get(row.user_id)
            if user is None or not user.is_active:
                return None
            return AccessToken(
                token=token,
                client_id=row.oauth_client_id or "",
                scopes=[SCOPE],
                expires_at=int(row.expires_at.timestamp()),
                subject=row.user_id,
            )

        access: AccessToken | None = await self._run(_load)
        return access

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        def _revoke(s: Any) -> None:
            if isinstance(token, AccessToken):
                row = AgentTokenRepository(s).get_by_token(token.token)
                if row is not None:
                    AgentTokenRepository(s).revoke(row)
                return
            refresh = repo.get_refresh_token(s, token.token)
            if refresh is not None:
                repo.revoke_refresh_token(s, refresh)

        await self._run(_revoke)


def _issue_access_token(
    session: Any, *, user_id: str, client_id: str, client: OAuthClientInformationFull
) -> str:
    """Створює рядок `agent_tokens` і повертає сире значення.

    `role=member`, а не роль людини: стеля самого токена. Реальні права
    все одно звужуються до `min(member, поточна роль власника)` на кожен
    запит, тож viewer лишається viewer'ом, а адмін свідомо НЕ отримує
    адмінських прав через конектор — батч-операції й керування
    користувачами не мають бути доступні чат-асистенту.
    """
    name = client.client_name or "claude.ai"
    row, raw = AgentTokenRepository(session).create(
        user_id=user_id,
        label=f"{_CONNECTOR_LABEL_PREFIX} ({name})"[:200],
        role=UserRole.member,
        expires_in_days=ACCESS_TOKEN_DAYS,
    )
    row.oauth_client_id = client_id
    session.flush()
    return raw


def build_redirect(pending: Any, code: str) -> str:
    """URL, на який відправляємо браузер назад у Claude після Approve."""
    params: dict[str, str] = {"code": code}
    if pending.state:
        params["state"] = pending.state
    return construct_redirect_uri(pending.redirect_uri, **params)


__all__ = [
    "ACCESS_TOKEN_DAYS",
    "SCOPE",
    "YovthOAuthProvider",
    "build_redirect",
]
