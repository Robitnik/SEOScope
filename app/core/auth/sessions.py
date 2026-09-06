"""Резолв cookie → користувач (task-71): найгарячіший шлях у системі.

Виконується на КОЖЕН запит, тому тут свідомо мінімум роботи: один
індексований SELECT по `token_hash` + один JOIN-у-вигляді-`get` по `users`.
Запис (`last_seen_at`) робиться не частіше, ніж раз на
`session_touch_interval_seconds`, і його провал ніколи не валить запит —
після task-66/67 будь-який зайвий запис у гарячому шляху коштує локом усій
системі, і сесія не та річ, заради якої варто його брати.
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.auth.policy import min_role
from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.db.base import utcnow
from app.db.repositories.agent_tokens import AgentTokenRepository
from app.db.repositories.users import UserRepository
from app.db.session import get_async_sessionmaker
from app.schemas.enums import UserRole

if TYPE_CHECKING:
    from starlette.requests import Request

logger = get_logger(__name__)


@dataclass(frozen=True)
class AuthenticatedSession:
    user_id: str
    email: str | None
    name: str
    role: UserRole
    is_active: bool
    session_id: str | None
    csrf_token: str
    # "cookie" (звичайний людський логін) | "agent" (dev bearer-токен, task-73,
    # АБО особистий токен, task-79 — обидва «не браузер», журнал відрізняє
    # їх за `actor_id`: dev-токен завжди `user_id="agent"`, особистий несе
    # РЕАЛЬНИЙ `user_id` власника) | "local" (довірений localhost без входу,
    # task-73). Визначає, чи взагалі є що звіряти в CSRF (`session_id is
    # None` — немає, бо жоден браузер не міг би підробити жоден із цих
    # способів автентифікації) і як рядок виглядає в журналі дій (`Actor.kind`).
    origin: str = "cookie"
    # Мітка особистого токена (task-79) — потрапляє в `audit_events.details`,
    # щоб рядок журналу читався як «Іван — via token "claude-code"», а не
    # просто «Іван». `None` для cookie-сесій і dev bearer-токена.
    token_label: str | None = None


def resolve_session_sync(
    session: Session, token: str, *, settings: Settings | None = None, now: datetime | None = None
) -> AuthenticatedSession | None:
    """Синхронне ядро (репозиторний шар). `None` = сесії немає, вона протухла,
    відкликана, або користувача деактивували — усі чотири випадки для
    викликача однакові й дають 401."""
    settings = settings or get_settings()
    moment = now or utcnow()
    repo = UserRepository(session)
    row = repo.get_session_by_token(token)
    if row is None or row.revoked_at is not None:
        return None
    if row.expires_at <= moment:
        return None
    idle_deadline = row.last_seen_at + timedelta(hours=settings.session_idle_ttl_hours)
    if idle_deadline <= moment:
        return None
    user = repo.get(row.user_id)
    if user is None or not user.is_active:
        return None

    touch_due = (
        moment - row.last_seen_at
    ).total_seconds() >= settings.session_touch_interval_seconds
    if touch_due:
        try:
            repo.touch_session(row, now=moment)
            session.commit()
        except SQLAlchemyError as exc:
            # Не даємо базі, що зайнята записом, завалити читання дашборда.
            session.rollback()
            logger.warning("session_touch_failed session_id=%s error=%s", row.id, exc)

    return AuthenticatedSession(
        user_id=user.id,
        email=user.email,
        name=user.name,
        role=user.role,
        is_active=user.is_active,
        session_id=row.id,
        csrf_token=row.csrf_token,
    )


async def resolve_session(
    token: str, *, settings: Settings | None = None
) -> AuthenticatedSession | None:
    """Async-обгортка для middleware: власна короткоживуча сесія БД.

    Своя сесія, а не `Depends(get_session)`, бо middleware виконується ДО
    того, як FastAPI взагалі побудує граф залежностей роутера.
    """
    settings = settings or get_settings()
    async with get_async_sessionmaker()() as db:
        return await db.run_sync(lambda s: resolve_session_sync(s, token, settings=settings))


# ── task-73: два dev-only способи автентифікації без cookie-логіну ─────────
#
# Обидва мертві поза `environment != "production"` — не «вимкнено за
# замовчуванням», а структурно неможливі: навіть якщо секрет чи прапорець
# випадково потраплять у продовий `.env`, ця перевірка все одно відмовляє.


def resolve_agent_token(request: Request, settings: Settings) -> AuthenticatedSession | None:
    """Спільний bearer-токен для паралельних агентів-розробників (task-73 §2.2).

    Жодного звернення до БД: constant-time звірка статичного секрету з
    `.env` — дешевше й простіше за cookie-шлях, і саме тому не годиться для
    людей (токен один на всіх, без відклику, без сесій).
    """
    if settings.environment == "production" or not settings.agent_dev_token:
        return None
    header = request.headers.get("authorization", "")
    if not header.startswith("Bearer "):
        return None
    token = header.removeprefix("Bearer ").strip()
    if not token or not hmac.compare_digest(token, settings.agent_dev_token):
        return None
    return AuthenticatedSession(
        user_id="agent",
        email=None,
        name=settings.agent_dev_token_label,
        role=settings.agent_dev_token_role,
        is_active=True,
        session_id=None,
        csrf_token="",
        origin="agent",
    )


def resolve_personal_token_sync(
    session: Session, token: str, *, settings: Settings | None = None, now: datetime | None = None
) -> AuthenticatedSession | None:
    """Особистий bearer-токен (task-79) — `AGENT_DEV_TOKEN`, але виданий
    людині собі самій у `#/account`, а не одним спільним секретом у `.env`.

    Синхронне ядро, той самий патерн, що `resolve_session_sync`: один
    індексований `SELECT` по хешу, один `get` по `users`, троттлений запис
    `last_used_at`, що ніколи не валить запит.

    Фактична роль — `min(роль токена, поточна роль власника)`: пониження
    людини чи `is_active=False` вбиває токен НЕГАЙНО, без окремого механізму
    відкликання (план §3.1–3.2) — токен ніколи не має пережити те, що вже
    заборонено самому власнику.
    """
    settings = settings or get_settings()
    moment = now or utcnow()
    repo = AgentTokenRepository(session)
    row = repo.get_by_token(token)
    if row is None or row.revoked_at is not None:
        return None
    if row.expires_at <= moment:
        return None
    user = UserRepository(session).get(row.user_id)
    if user is None or not user.is_active:
        return None

    touch_due = (
        row.last_used_at is None
        or (moment - row.last_used_at).total_seconds() >= settings.session_touch_interval_seconds
    )
    if touch_due:
        try:
            repo.touch(row, now=moment)
            session.commit()
        except SQLAlchemyError as exc:
            session.rollback()
            logger.warning("agent_token_touch_failed token_id=%s error=%s", row.id, exc)

    return AuthenticatedSession(
        user_id=user.id,
        email=user.email,
        name=user.name,
        role=min_role(row.role, user.role),
        is_active=True,
        session_id=None,
        csrf_token="",
        origin="agent",
        token_label=row.label,
    )


async def resolve_personal_token(
    request: Request, *, settings: Settings | None = None
) -> AuthenticatedSession | None:
    """Async-обгортка для middleware — той самий патерн, що `resolve_session`.

    Дешевий вихід ще ДО будь-якого звернення до БД: немає `Bearer`-заголовка
    (звичайний браузерний запит із cookie) — жодного запиту в базу.
    """
    header = request.headers.get("authorization", "")
    if not header.startswith("Bearer "):
        return None
    token = header.removeprefix("Bearer ").strip()
    if not token:
        return None
    settings = settings or get_settings()
    async with get_async_sessionmaker()() as db:
        return await db.run_sync(
            lambda s: resolve_personal_token_sync(s, token, settings=settings)
        )


# Заголовки, які ставить лише проксі-шлях (Cloudflare Tunnel/Access) — і які
# ФІЗИЧНО не можуть з'явитися на з'єднанні, що прийшло прямим TCP-хендшейком
# від браузера на localhost, бо між ними немає нічого, що їх додало б.
# `network_mode: host` (compose.yaml) означає, що `cloudflared`, форвардячи
# публічний тунель на `127.0.0.1:6524`, дає РІВНО ТУ САМУ адресу джерела, що
# й людина, яка відкрила `http://localhost:6524` у своєму браузері — тому
# сама по собі адреса localhost нічого не доводить, і рішення тримається на
# ВІДСУТНОСТІ цих заголовків, а не на адресі окремо.
_PROXY_MARKER_HEADERS = (
    "cf-connecting-ip",
    "cf-ray",
    "cf-visitor",
    "cf-ipcountry",
    "x-forwarded-for",
    "x-forwarded-proto",
    "x-forwarded-host",
    "forwarded",
)


def is_trusted_loopback(request: Request) -> bool:
    host = request.client.host if request.client else None
    if host not in {"127.0.0.1", "::1"}:
        return False
    return not any(header in request.headers for header in _PROXY_MARKER_HEADERS)


def resolve_localhost_bypass(request: Request, settings: Settings) -> AuthenticatedSession | None:
    """Вхід без пароля для оператора на його власній машині (task-73 §8).

    Спрацьовує ЛИШЕ на прямому TCP-з'єднанні з loopback без жодного сліду
    проксі (`is_trusted_loopback`) — інакше публічний Cloudflare-тунель, що
    форвардить рівно на цю саму адресу, отримав би доступ без входу разом
    із оператором, і вся робота task-71 знецінилась би.
    """
    if settings.environment == "production" or not settings.auth_localhost_bypass:
        return None
    if not is_trusted_loopback(request):
        return None
    return AuthenticatedSession(
        user_id="local",
        email=None,
        name="Local operator",
        role=UserRole.admin,
        is_active=True,
        session_id=None,
        csrf_token="",
        origin="local",
    )
