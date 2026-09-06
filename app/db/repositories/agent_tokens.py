"""Особисті токени (task-79) — «кожен видає собі сам».

Дзеркалить `UserRepository`'s сесійний шар (`hash_token`, «сирий токен існує
лише в цьому виклику») буквально — той самий принцип зберігання, лише інша
таблиця й довший термін життя.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.logging import add_redact_patterns
from app.db.base import utcnow
from app.db.models import AgentToken
from app.db.repositories.users import hash_token
from app.schemas.enums import UserRole

# 32 випадкових байти — той самий бюджет ентропії, що й у сесійних токенів
# (`app/db/repositories/users.py::_TOKEN_BYTES`).
_TOKEN_BYTES = 32
_PREFIX = "yvth_"

# Реєструється на момент імпорту, той самий патерн, що `app/cloud/client.py`:
# ключове (`_DEFAULT_REDACT_KEYS`) редагування ловить значення під ключем
# `token`/`authorization`, але не саме значення, вклеєне у вільний текст
# (наприклад, у повідомлення помилки) — цей патерн ловить рівно те, що
# перше не бачить.
add_redact_patterns([r"yvth_[A-Za-z0-9_-]+"])


def generate_token() -> tuple[str, str]:
    """Повертає `(сире значення, prefix)`. Префікс `yvth_` — не косметика:
    робить токен упізнаваним у чужих логах і придатним до автопошуку витоків
    (`grep -r "yvth_"`), а перші 8 символів після нього — НЕ секрет, лише
    для впізнавання свого рядка в списку без другого показу повного значення."""
    raw = secrets.token_urlsafe(_TOKEN_BYTES)
    token = f"{_PREFIX}{raw}"
    return token, raw[:8]


class AgentTokenRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def create(
        self,
        *,
        user_id: str,
        label: str,
        role: UserRole,
        expires_in_days: int,
    ) -> tuple[AgentToken, str]:
        """Повертає `(рядок, СИРИЙ токен)`. Сирий токен існує лише в цьому
        виклику й у HTTP-відповіді — у БД його немає ніде."""
        token, prefix = generate_token()
        row = AgentToken(
            user_id=user_id,
            label=label.strip(),
            token_sha256=hash_token(token),
            prefix=prefix,
            role=role,
            expires_at=utcnow() + timedelta(days=expires_in_days),
        )
        self._session.add(row)
        self._session.flush()
        return row, token

    def get_by_token(self, token: str) -> AgentToken | None:
        return self._session.scalars(
            select(AgentToken).where(AgentToken.token_sha256 == hash_token(token))
        ).first()

    def list_for_user(self, user_id: str) -> list[AgentToken]:
        return list(
            self._session.scalars(
                select(AgentToken)
                .where(AgentToken.user_id == user_id)
                .order_by(AgentToken.created_at.desc())
            )
        )

    def get_for_user(self, token_id: str, user_id: str) -> AgentToken | None:
        return self._session.scalars(
            select(AgentToken).where(
                AgentToken.id == token_id, AgentToken.user_id == user_id
            )
        ).first()

    def revoke(self, row: AgentToken, *, now: datetime | None = None) -> None:
        row.revoked_at = now or utcnow()
        self._session.flush()

    def touch(self, row: AgentToken, *, now: datetime | None = None) -> None:
        row.last_used_at = now or utcnow()
        self._session.flush()
