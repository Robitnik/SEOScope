"""Життєвий цикл сесії (task-71): протухання, відкликання, троттлінг запису."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy.orm import Session

from app.core.auth.sessions import resolve_session_sync
from app.core.config import Settings
from app.db.base import utcnow
from app.db.models import User
from app.db.repositories.users import UserRepository, hash_token
from app.schemas.enums import UserRole


def _settings(**overrides: object) -> Settings:
    return Settings(**overrides)  # type: ignore[arg-type]


def _user(db: Session, *, role: UserRole = UserRole.member) -> User:
    repo = UserRepository(db)
    user = repo.create(email="ivan@yovth.com", name="Ivan", role=role, password="pw-123")
    db.commit()
    return user


def test_token_is_never_stored_in_plaintext(db: Session) -> None:
    repo = UserRepository(db)
    user = _user(db)
    row, token = repo.create_session(user, absolute_ttl_days=14)
    db.commit()
    assert token not in row.token_hash
    assert row.token_hash == hash_token(token)


def test_resolve_returns_the_user_for_a_live_session(db: Session) -> None:
    repo = UserRepository(db)
    user = _user(db, role=UserRole.admin)
    _, token = repo.create_session(user, absolute_ttl_days=14)
    db.commit()

    resolved = resolve_session_sync(db, token, settings=_settings())
    assert resolved is not None
    assert resolved.email == "ivan@yovth.com"
    assert resolved.role == UserRole.admin


def test_unknown_token_resolves_to_nothing(db: Session) -> None:
    assert resolve_session_sync(db, "not-a-real-token", settings=_settings()) is None


def test_revoked_session_stops_working_immediately(db: Session) -> None:
    repo = UserRepository(db)
    user = _user(db)
    row, token = repo.create_session(user, absolute_ttl_days=14)
    db.commit()
    repo.revoke_session(row)
    db.commit()
    assert resolve_session_sync(db, token, settings=_settings()) is None


def test_absolute_expiry_ends_the_session(db: Session) -> None:
    repo = UserRepository(db)
    user = _user(db)
    row, token = repo.create_session(user, absolute_ttl_days=14)
    db.commit()

    later = row.expires_at + timedelta(seconds=1)
    assert resolve_session_sync(db, token, settings=_settings(), now=later) is None


def test_idle_expiry_ends_the_session(db: Session) -> None:
    """Абсолютний строк ще не вийшов, але сесією не користувались."""
    repo = UserRepository(db)
    user = _user(db)
    _, token = repo.create_session(user, absolute_ttl_days=14)
    db.commit()

    settings = _settings(session_idle_ttl_hours=1)
    later = utcnow() + timedelta(hours=2)
    assert resolve_session_sync(db, token, settings=settings, now=later) is None


def test_deactivated_user_cannot_use_an_existing_session(db: Session) -> None:
    repo = UserRepository(db)
    user = _user(db)
    _, token = repo.create_session(user, absolute_ttl_days=14)
    db.commit()
    repo.update_profile(user, is_active=False)
    db.commit()
    assert resolve_session_sync(db, token, settings=_settings()) is None


def test_last_seen_is_throttled(db: Session) -> None:
    """Найважливіше в цьому файлі: сесія НЕ пише в БД на кожен запит.

    Після task-66/67 (writer-лок був зайнятий 97% часу) запис на кожен запит
    був би податком на всю систему.
    """
    repo = UserRepository(db)
    user = _user(db)
    row, token = repo.create_session(user, absolute_ttl_days=14)
    db.commit()
    original = row.last_seen_at
    settings = _settings(session_touch_interval_seconds=300)

    resolve_session_sync(db, token, settings=settings, now=original + timedelta(seconds=30))
    db.refresh(row)
    assert row.last_seen_at == original, "запис у межах вікна троттлінгу не робиться"

    moment = original + timedelta(seconds=301)
    resolve_session_sync(db, token, settings=settings, now=moment)
    db.refresh(row)
    assert row.last_seen_at == moment, "після вікна `last_seen_at` оновлюється"


def test_lockout_after_repeated_failures(db: Session) -> None:
    repo = UserRepository(db)
    user = _user(db)
    for _ in range(3):
        repo.register_failure(user, max_failures=3, lockout_minutes=15)
    db.commit()
    assert repo.is_locked(user) is True
    assert repo.is_locked(user, now=utcnow() + timedelta(minutes=16)) is False


def test_revoke_all_can_keep_the_current_session(db: Session) -> None:
    repo = UserRepository(db)
    user = _user(db)
    keep, keep_token = repo.create_session(user, absolute_ttl_days=14)
    _, other_token = repo.create_session(user, absolute_ttl_days=14)
    db.commit()

    assert repo.revoke_all_sessions(user.id, except_session_id=keep.id) == 1
    db.commit()
    assert resolve_session_sync(db, keep_token, settings=_settings()) is not None
    assert resolve_session_sync(db, other_token, settings=_settings()) is None


def test_purge_removes_only_expired_rows(db: Session) -> None:
    repo = UserRepository(db)
    user = _user(db)
    row, _ = repo.create_session(user, absolute_ttl_days=14)
    db.commit()
    assert repo.purge_expired_sessions() == 0
    assert repo.purge_expired_sessions(now=row.expires_at + timedelta(seconds=1)) == 1


@pytest.mark.parametrize("role", list(UserRole))
def test_every_role_round_trips_through_the_database(db: Session, role: UserRole) -> None:
    repo = UserRepository(db)
    user = repo.create(email=f"{role.value}@yovth.com", name=role.value, role=role, password="pw")
    db.commit()
    assert repo.get(user.id).role == role
