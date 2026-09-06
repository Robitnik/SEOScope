"""Резолв cookie → користувач (task-71): найгарячіший шлях у системі.

Виконується на КОЖЕН запит, тому тут свідомо мінімум роботи: один
індексований SELECT по `token_hash` + один JOIN-у-вигляді-`get` по `users`.
Запис (`last_seen_at`) робиться не частіше, ніж раз на
`session_touch_interval_seconds`, і його провал ніколи не валить запит —
після task-66/67 будь-який зайвий запис у гарячому шляху коштує локом усій
системі, і сесія не та річ, заради якої варто його брати.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.db.base import utcnow
from app.db.repositories.users import UserRepository
from app.db.session import get_async_sessionmaker
from app.schemas.enums import UserRole

logger = get_logger(__name__)


@dataclass(frozen=True)
class AuthenticatedSession:
    user_id: str
    email: str
    name: str
    role: UserRole
    is_active: bool
    session_id: str
    csrf_token: str


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
