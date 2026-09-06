"""Особисті токени (task-79): резолв, стеля ролі, деактивація, троттлінг.

Той самий харнес, що `test_sessions.py` — `AgentToken` дзеркалить
`UserSession` буквально, тестується так само.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy.orm import Session

from app.core.auth.policy import min_role
from app.core.auth.sessions import resolve_personal_token_sync
from app.core.config import Settings
from app.db.base import utcnow
from app.db.models import User
from app.db.repositories.agent_tokens import AgentTokenRepository
from app.db.repositories.users import UserRepository, hash_token
from app.schemas.enums import UserRole


def _settings(**overrides: object) -> Settings:
    return Settings(**overrides)  # type: ignore[arg-type]


def _user(db: Session, *, role: UserRole = UserRole.member, email: str = "ivan@yovth.com") -> User:
    repo = UserRepository(db)
    user = repo.create(email=email, name="Ivan", role=role, password="pw-123")
    db.commit()
    return user


def test_token_is_never_stored_in_plaintext(db: Session) -> None:
    repo = AgentTokenRepository(db)
    user = _user(db)
    row, token = repo.create(user_id=user.id, label="ci", role=UserRole.member, expires_in_days=90)
    db.commit()
    assert token not in row.token_sha256
    assert row.token_sha256 == hash_token(token)
    assert token.startswith("yvth_")
    assert row.prefix == token.removeprefix("yvth_")[:8]


def test_resolve_returns_the_owner_with_matching_role(db: Session) -> None:
    repo = AgentTokenRepository(db)
    user = _user(db, role=UserRole.admin)
    _, token = repo.create(user_id=user.id, label="ci", role=UserRole.admin, expires_in_days=90)
    db.commit()

    resolved = resolve_personal_token_sync(db, token, settings=_settings())
    assert resolved is not None
    assert resolved.user_id == user.id
    assert resolved.email == "ivan@yovth.com"
    assert resolved.role == UserRole.admin
    assert resolved.origin == "agent"
    assert resolved.token_label == "ci"


def test_unknown_token_resolves_to_nothing(db: Session) -> None:
    assert resolve_personal_token_sync(db, "yvth_not-a-real-token", settings=_settings()) is None


def test_expired_token_stops_working(db: Session) -> None:
    repo = AgentTokenRepository(db)
    user = _user(db)
    row, token = repo.create(user_id=user.id, label="ci", role=UserRole.member, expires_in_days=1)
    db.commit()

    later = row.expires_at + timedelta(seconds=1)
    assert resolve_personal_token_sync(db, token, settings=_settings(), now=later) is None


def test_revoked_token_stops_working_immediately(db: Session) -> None:
    repo = AgentTokenRepository(db)
    user = _user(db)
    row, token = repo.create(user_id=user.id, label="ci", role=UserRole.member, expires_in_days=90)
    db.commit()
    repo.revoke(row)
    db.commit()
    assert resolve_personal_token_sync(db, token, settings=_settings()) is None


def test_deactivated_owner_kills_the_token(db: Session) -> None:
    """§3.2 плану: `is_active=False` вбиває ВСІ токени цієї людини негайно."""
    user_repo = UserRepository(db)
    token_repo = AgentTokenRepository(db)
    user = _user(db)
    _, token = token_repo.create(
        user_id=user.id, label="ci", role=UserRole.member, expires_in_days=90
    )
    db.commit()
    user_repo.update_profile(user, is_active=False)
    db.commit()
    assert resolve_personal_token_sync(db, token, settings=_settings()) is None


def test_role_is_recomputed_live_not_frozen_at_creation(db: Session) -> None:
    """§3.1 плану: пониження ролі власника діє того ж запиту, без нового
    механізму відкликання — токен ніколи не переживає те, що вже заборонено
    самому власнику."""
    user_repo = UserRepository(db)
    token_repo = AgentTokenRepository(db)
    user = _user(db, role=UserRole.admin)
    _, token = token_repo.create(
        user_id=user.id, label="ci", role=UserRole.admin, expires_in_days=90
    )
    db.commit()

    resolved = resolve_personal_token_sync(db, token, settings=_settings())
    assert resolved is not None
    assert resolved.role == UserRole.admin

    user_repo.update_profile(user, role=UserRole.viewer)
    db.commit()

    resolved_after = resolve_personal_token_sync(db, token, settings=_settings())
    assert resolved_after is not None
    assert resolved_after.role == UserRole.viewer, "учорашній admin-токен сьогодні є viewer-токеном"


@pytest.mark.parametrize(
    ("token_role", "owner_role", "expected"),
    [
        (UserRole.admin, UserRole.viewer, UserRole.viewer),  # не можна собі більше, ніж маєш
        (UserRole.viewer, UserRole.admin, UserRole.viewer),  # можна собі менше — заохочено
        (UserRole.member, UserRole.member, UserRole.member),
    ],
)
def test_effective_role_is_the_lesser_of_token_and_owner(
    token_role: UserRole, owner_role: UserRole, expected: UserRole
) -> None:
    assert min_role(token_role, owner_role) == expected


def test_last_used_is_throttled(db: Session) -> None:
    """Той самий принцип, що сесії (task-66/67): запис на кожен запит —
    податок на writer-лок усієї системи, не лише цього токена."""
    repo = AgentTokenRepository(db)
    user = _user(db)
    row, token = repo.create(user_id=user.id, label="ci", role=UserRole.member, expires_in_days=90)
    db.commit()
    settings = _settings(session_touch_interval_seconds=300)

    first_moment = utcnow() + timedelta(seconds=10)
    resolve_personal_token_sync(db, token, settings=settings, now=first_moment)
    db.refresh(row)
    assert row.last_used_at == first_moment

    resolve_personal_token_sync(db, token, settings=settings, now=first_moment + timedelta(seconds=30))
    db.refresh(row)
    assert row.last_used_at == first_moment, "запис у межах вікна троттлінгу не робиться"

    later = first_moment + timedelta(seconds=301)
    resolve_personal_token_sync(db, token, settings=settings, now=later)
    db.refresh(row)
    assert row.last_used_at == later, "після вікна `last_used_at` оновлюється"


def test_two_users_tokens_are_independent(db: Session) -> None:
    repo = AgentTokenRepository(db)
    alice = _user(db, email="alice@yovth.com")
    bob = _user(db, email="bob@yovth.com")
    _, alice_token = repo.create(
        user_id=alice.id, label="alice-ci", role=UserRole.member, expires_in_days=90
    )
    _, bob_token = repo.create(
        user_id=bob.id, label="bob-ci", role=UserRole.member, expires_in_days=90
    )
    db.commit()

    assert repo.get_for_user(repo.list_for_user(alice.id)[0].id, bob.id) is None
    resolved = resolve_personal_token_sync(db, alice_token, settings=_settings())
    assert resolved is not None
    assert resolved.email == "alice@yovth.com"
    resolved = resolve_personal_token_sync(db, bob_token, settings=_settings())
    assert resolved is not None
    assert resolved.email == "bob@yovth.com"
