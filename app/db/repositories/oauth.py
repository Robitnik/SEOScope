"""Сховище OAuth-потоку для remote-конектора (task-78).

Синхронні функції над `Session` — рівно як у решті репозиторіїв проєкту;
async-обгортки живуть у `app/mcp/oauth.py`, де вони й потрібні.

Спільний принцип із `agent_tokens` і `user_sessions`: у БД лежить лише
sha256 будь-якого пред'явницького значення (код авторизації, refresh),
сире існує рівно в одній HTTP-відповіді. sha256, а не scrypt — це 256
випадкових біт, а не людський пароль: розтягування нічого не додає, а
перевірка на кожен запит має бути дешевою.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.base import utcnow
from app.db.models import (
    OAuthAuthorizationCode,
    OAuthClient,
    OAuthPendingAuthorization,
    OAuthRefreshToken,
)

# Код авторизації живе хвилини — його єдина робота це один редірект
# назад у Claude і один POST /token одразу за ним.
AUTH_CODE_TTL = timedelta(minutes=5)
# Стільки людина має на логін і натискання Approve.
PENDING_TTL = timedelta(minutes=15)
REFRESH_TTL = timedelta(days=90)


def hash_secret(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def new_secret() -> str:
    return secrets.token_urlsafe(32)


# --- клієнти (DCR) ---------------------------------------------------------


def save_client(db: Session, client_id: str, payload: dict[str, Any]) -> OAuthClient:
    row = db.get(OAuthClient, client_id)
    if row is None:
        row = OAuthClient(client_id=client_id, client_json=json.dumps(payload))
        db.add(row)
    else:
        row.client_json = json.dumps(payload)
    db.flush()
    return row


def load_client(db: Session, client_id: str) -> dict[str, Any] | None:
    row = db.get(OAuthClient, client_id)
    if row is None:
        return None
    parsed = json.loads(row.client_json)
    return parsed if isinstance(parsed, dict) else None


# --- очікування згоди ------------------------------------------------------


def create_pending(
    db: Session,
    *,
    client_id: str,
    scopes: list[str],
    code_challenge: str,
    redirect_uri: str,
    redirect_uri_provided_explicitly: bool,
    state: str | None,
    resource: str | None,
    now: datetime | None = None,
) -> OAuthPendingAuthorization:
    now = now or utcnow()
    row = OAuthPendingAuthorization(
        client_id=client_id,
        scopes=" ".join(scopes),
        code_challenge=code_challenge,
        redirect_uri=redirect_uri,
        redirect_uri_provided_explicitly=redirect_uri_provided_explicitly,
        state=state,
        resource=resource,
        created_at=now,
        expires_at=now + PENDING_TTL,
    )
    db.add(row)
    db.flush()
    return row


def get_pending(
    db: Session, pending_id: str, *, now: datetime | None = None
) -> OAuthPendingAuthorization | None:
    row = db.get(OAuthPendingAuthorization, pending_id)
    if row is None or row.completed_at is not None:
        return None
    if row.expires_at <= (now or utcnow()):
        return None
    return row


# --- коди авторизації ------------------------------------------------------


def issue_auth_code(
    db: Session,
    *,
    pending: OAuthPendingAuthorization,
    user_id: str,
    now: datetime | None = None,
) -> str:
    """Завершує очікування й повертає СИРИЙ код (єдиний раз, коли він існує)."""
    now = now or utcnow()
    raw = new_secret()
    db.add(
        OAuthAuthorizationCode(
            code_sha256=hash_secret(raw),
            client_id=pending.client_id,
            user_id=user_id,
            scopes=pending.scopes,
            code_challenge=pending.code_challenge,
            redirect_uri=pending.redirect_uri,
            redirect_uri_provided_explicitly=pending.redirect_uri_provided_explicitly,
            resource=pending.resource,
            created_at=now,
            expires_at=now + AUTH_CODE_TTL,
        )
    )
    pending.completed_at = now
    db.flush()
    return raw


def get_auth_code(
    db: Session, code: str, *, now: datetime | None = None
) -> OAuthAuthorizationCode | None:
    row = db.execute(
        select(OAuthAuthorizationCode).where(
            OAuthAuthorizationCode.code_sha256 == hash_secret(code)
        )
    ).scalar_one_or_none()
    if row is None or row.used_at is not None:
        return None
    if row.expires_at <= (now or utcnow()):
        return None
    return row


def consume_auth_code(
    db: Session, row: OAuthAuthorizationCode, *, now: datetime | None = None
) -> None:
    """Одноразовість коду — на рівні даних, не на рівні доброї волі викликача."""
    row.used_at = now or utcnow()
    db.flush()


# --- refresh-токени --------------------------------------------------------


def issue_refresh_token(
    db: Session,
    *,
    client_id: str,
    user_id: str,
    scopes: str,
    now: datetime | None = None,
) -> str:
    now = now or utcnow()
    raw = new_secret()
    db.add(
        OAuthRefreshToken(
            token_sha256=hash_secret(raw),
            client_id=client_id,
            user_id=user_id,
            scopes=scopes,
            created_at=now,
            expires_at=now + REFRESH_TTL,
        )
    )
    db.flush()
    return raw


def get_refresh_token(
    db: Session, token: str, *, now: datetime | None = None
) -> OAuthRefreshToken | None:
    row = db.execute(
        select(OAuthRefreshToken).where(OAuthRefreshToken.token_sha256 == hash_secret(token))
    ).scalar_one_or_none()
    if row is None or row.revoked_at is not None:
        return None
    if row.expires_at <= (now or utcnow()):
        return None
    return row


def revoke_refresh_token(
    db: Session, row: OAuthRefreshToken, *, now: datetime | None = None
) -> None:
    row.revoked_at = now or utcnow()
    db.flush()
