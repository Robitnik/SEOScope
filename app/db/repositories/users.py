"""Користувачі й сесії (task-71).

Як і всі репозиторії — проти синхронної `Session`; API дістається сюди через
`AsyncSession.run_sync` (див. `app/db/repositories/__init__.py`).

Правило шару: назовні НІКОЛИ не віддається `password_hash` — виклики
працюють або з ORM-об'єктом усередині запиту, або з DTO у `app/api/*`.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta
from typing import Any, cast

from sqlalchemy import CursorResult, delete, func, select, update
from sqlalchemy.orm import Session

from app.core.auth.passwords import hash_password
from app.db.base import utcnow
from app.db.models import User, UserSession
from app.schemas.auth import normalize_email
from app.schemas.enums import UserRole

# Довжина сирого токена сесії в байтах. У БД лежить лише його sha256.
_TOKEN_BYTES = 32


def hash_token(token: str) -> str:
    """sha256, а не scrypt: токен — це 256 випадкових біт, а не пароль людини.

    Перебирати його немає сенсу, тож повільний KDF тут дав би лише затримку
    на КОЖЕН запит (перевірка сесії — найгарячіший шлях у системі).
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class UserRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    # ── користувачі ──────────────────────────────────────────────────────

    def get(self, user_id: str) -> User | None:
        return self._session.get(User, user_id)

    def get_by_email(self, email: str) -> User | None:
        try:
            normalized = normalize_email(email)
        except ValueError:
            return None
        return self._session.scalars(select(User).where(User.email == normalized)).first()

    def list_all(self) -> list[User]:
        return list(self._session.scalars(select(User).order_by(User.name)))

    def count(self) -> int:
        return int(self._session.scalar(select(func.count()).select_from(User)) or 0)

    def count_active_admins(self, *, excluding: str | None = None) -> int:
        stmt = select(func.count()).select_from(User).where(
            User.role == UserRole.admin, User.is_active.is_(True)
        )
        if excluding is not None:
            stmt = stmt.where(User.id != excluding)
        return int(self._session.scalar(stmt) or 0)

    def create(
        self,
        *,
        email: str,
        name: str,
        role: UserRole,
        password: str,
        created_by_id: str | None = None,
    ) -> User:
        user = User(
            email=normalize_email(email),
            name=name.strip(),
            role=role,
            password_hash=hash_password(password),
            created_by_id=created_by_id,
        )
        self._session.add(user)
        self._session.flush()
        return user

    def set_password(self, user: User, password: str) -> None:
        user.password_hash = hash_password(password)
        user.password_set_at = utcnow()
        user.failed_attempts = 0
        user.locked_until = None
        self._session.flush()

    def update_profile(
        self,
        user: User,
        *,
        name: str | None = None,
        role: UserRole | None = None,
        is_active: bool | None = None,
    ) -> User:
        if name is not None:
            user.name = name.strip()
        if role is not None:
            user.role = role
        if is_active is not None:
            user.is_active = is_active
        self._session.flush()
        return user

    # ── лічильник невдалих спроб ─────────────────────────────────────────

    def register_failure(self, user: User, *, max_failures: int, lockout_minutes: int) -> None:
        user.failed_attempts += 1
        if user.failed_attempts >= max_failures:
            user.locked_until = utcnow() + timedelta(minutes=lockout_minutes)
            user.failed_attempts = 0
        self._session.flush()

    def register_success(self, user: User, *, now: datetime | None = None) -> None:
        user.failed_attempts = 0
        user.locked_until = None
        user.last_login_at = now or utcnow()
        self._session.flush()

    @staticmethod
    def is_locked(user: User, *, now: datetime | None = None) -> bool:
        if user.locked_until is None:
            return False
        return user.locked_until > (now or utcnow())

    # ── сесії ────────────────────────────────────────────────────────────

    def create_session(
        self,
        user: User,
        *,
        absolute_ttl_days: int,
        ip: str | None = None,
        user_agent: str | None = None,
    ) -> tuple[UserSession, str]:
        """Повертає `(рядок сесії, СИРИЙ токен)`. Сирий токен існує лише в
        цьому виклику й у cookie — у БД його немає ніде."""
        token = secrets.token_urlsafe(_TOKEN_BYTES)
        session_row = UserSession(
            user_id=user.id,
            token_hash=hash_token(token),
            csrf_token=secrets.token_urlsafe(_TOKEN_BYTES),
            expires_at=utcnow() + timedelta(days=absolute_ttl_days),
            ip=ip,
            user_agent=(user_agent or "")[:256] or None,
        )
        self._session.add(session_row)
        self._session.flush()
        return session_row, token

    def get_session_by_token(self, token: str) -> UserSession | None:
        return self._session.scalars(
            select(UserSession).where(UserSession.token_hash == hash_token(token))
        ).first()

    def active_sessions(self, user_id: str, *, now: datetime | None = None) -> list[UserSession]:
        moment = now or utcnow()
        return list(
            self._session.scalars(
                select(UserSession)
                .where(
                    UserSession.user_id == user_id,
                    UserSession.revoked_at.is_(None),
                    UserSession.expires_at > moment,
                )
                .order_by(UserSession.last_seen_at.desc())
            )
        )

    def active_session_counts(self, *, now: datetime | None = None) -> dict[str, int]:
        moment = now or utcnow()
        rows = self._session.execute(
            select(UserSession.user_id, func.count())
            .where(UserSession.revoked_at.is_(None), UserSession.expires_at > moment)
            .group_by(UserSession.user_id)
        ).all()
        return {str(user_id): int(count) for user_id, count in rows}

    def touch_session(self, session_row: UserSession, *, now: datetime | None = None) -> None:
        session_row.last_seen_at = now or utcnow()
        self._session.flush()

    def revoke_session(self, session_row: UserSession, *, now: datetime | None = None) -> None:
        if session_row.revoked_at is None:
            session_row.revoked_at = now or utcnow()
            self._session.flush()

    def revoke_all_sessions(
        self, user_id: str, *, except_session_id: str | None = None, now: datetime | None = None
    ) -> int:
        stmt = (
            update(UserSession)
            .where(UserSession.user_id == user_id, UserSession.revoked_at.is_(None))
            .values(revoked_at=now or utcnow())
        )
        if except_session_id is not None:
            stmt = stmt.where(UserSession.id != except_session_id)
        result = cast("CursorResult[Any]", self._session.execute(stmt))
        self._session.flush()
        return int(result.rowcount or 0)

    def purge_expired_sessions(self, *, now: datetime | None = None) -> int:
        """Прибирає давно мертві рядки. Викликається щодобовим тіком
        `DbMaintenanceScheduler`, не з гарячого шляху запиту."""
        moment = now or utcnow()
        result = cast(
            "CursorResult[Any]",
            self._session.execute(delete(UserSession).where(UserSession.expires_at < moment)),
        )
        self._session.flush()
        return int(result.rowcount or 0)
