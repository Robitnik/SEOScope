"""`Attribution.from_actor` (task-93 plan §2 D2, §6 test list).

Pure function, no DB/session needed — every case below is a plain
`Actor` construction (`app/api/deps.py`) mapped to `(kind, user_id)`.
"""

from __future__ import annotations

from app.api.deps import Actor
from app.authorship.model import Attribution
from app.schemas.enums import UserRole

_REAL_USER_ID = "3f5a9c2e-1b4d-4a7f-9e12-0a1b2c3d4e5f"


def test_cookie_login_keeps_the_real_user_id() -> None:
    actor = Actor(
        id=_REAL_USER_ID, kind="user", email="olha@theyovth.com", name="Olha",
        role=UserRole.member, session_id="sess-1",
    )
    attribution = Attribution.from_actor(actor)
    assert attribution == Attribution(kind="user", user_id=_REAL_USER_ID)


def test_personal_token_is_agent_kind_with_the_owners_real_user_id() -> None:
    """task-79 personal token: `Actor.kind="agent"`, but `Actor.id` is the
    REAL owner's GUID — it must be preserved, not treated like the dev
    bearer token's synthetic `"agent"` id."""
    actor = Actor(
        id=_REAL_USER_ID, kind="agent", email="admin2@theyovth.com", name="Admin2",
        role=UserRole.admin,
    )
    attribution = Attribution.from_actor(actor)
    assert attribution == Attribution(kind="agent", user_id=_REAL_USER_ID)


def test_dev_bearer_token_is_agent_kind_with_no_user() -> None:
    """task-73 `AGENT_DEV_TOKEN`: `AuthenticatedSession.user_id="agent"` —
    never a real row in `users`, so `user_id` must stay `None`."""
    actor = Actor(id="agent", kind="agent", role=UserRole.admin, name="dev-agent")
    attribution = Attribution.from_actor(actor)
    assert attribution == Attribution(kind="agent", user_id=None)


def test_localhost_bypass_is_local_kind_with_no_user() -> None:
    actor = Actor(id="local", kind="local", role=UserRole.admin, name="Local operator")
    attribution = Attribution.from_actor(actor)
    assert attribution == Attribution(kind="local", user_id=None)


def test_system_actor_has_no_user() -> None:
    attribution = Attribution.from_actor(Actor.system())
    assert attribution == Attribution(kind="system", user_id=None)


def test_cli_actor_has_no_user() -> None:
    attribution = Attribution.from_actor(Actor.cli())
    assert attribution == Attribution(kind="cli", user_id=None)


def test_cli_classmethod_matches_from_actor_cli() -> None:
    assert Attribution.cli() == Attribution.from_actor(Actor.cli())


def test_unknown_kind_with_non_uuid_id_degrades_to_unknown_not_a_guessed_fk() -> None:
    """Defensive case: neither a real UUID nor one of the known
    non-user sentinel ids — must never end up in `user_id` (a bad FK is
    worse than an honest "unknown")."""
    actor = Actor(id="whatever-not-a-uuid", kind="something-new")
    attribution = Attribution.from_actor(actor)
    assert attribution == Attribution.unknown()


def test_unknown_returns_kind_none_and_user_id_none() -> None:
    assert Attribution.unknown() == Attribution(kind=None, user_id=None)
