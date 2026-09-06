"""Особисті токени крізь справжній HTTP-стек (task-79): «кожен видає собі
сам» у `#/account`."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.auth


def test_unauthenticated_request_is_rejected(auth_client: TestClient) -> None:
    assert auth_client.get("/api/v1/me/tokens").status_code == 401


def test_responses_are_never_cached(auth_client: TestClient, make_user, sign_in) -> None:
    """Знайдено вживу: без `no-store` браузер/проксі міг обслужити щойно
    відкликаний токен зі свого кешу на ідентичний GET — «діє негайно» (§3.2)
    ставало хибним саме там, де ціна помилки найвища."""
    email, password = make_user("ivan@yovth.com")
    headers = sign_in(email, password)
    response = auth_client.get("/api/v1/me/tokens", headers=headers)
    assert response.headers["cache-control"] == "no-store"


def test_create_shows_full_value_exactly_once(
    auth_client: TestClient, make_user, sign_in
) -> None:
    email, password = make_user("ivan@yovth.com")
    headers = sign_in(email, password)

    created = auth_client.post(
        "/api/v1/me/tokens",
        json={"label": "claude-code", "role": "member", "expires_in_days": 90},
        headers=headers,
    )
    assert created.status_code == 201
    body = created.json()
    token = body["token"]
    assert token.startswith("yvth_")
    assert body["prefix"] == token.removeprefix("yvth_")[:8]

    # Повне значення НІКОЛИ не з'являється в наступному читанні.
    listing = auth_client.get("/api/v1/me/tokens", headers=headers)
    assert listing.status_code == 200
    assert token not in listing.text
    rows = listing.json()
    assert len(rows) == 1
    assert rows[0]["label"] == "claude-code"
    assert "token" not in rows[0]


def test_the_token_actually_authenticates(auth_client: TestClient, make_user, sign_in) -> None:
    email, password = make_user("ivan@yovth.com", role="admin")
    headers = sign_in(email, password)
    created = auth_client.post(
        "/api/v1/me/tokens",
        json={"label": "ci", "role": "admin", "expires_in_days": 90},
        headers=headers,
    )
    token = created.json()["token"]

    response = auth_client.get(
        "/api/v1/me/tokens", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 200


def test_viewer_can_create_and_revoke_their_own_token(
    auth_client: TestClient, make_user, sign_in
) -> None:
    """§1/§3.3 плану: самообслуговування працює для КОЖНОЇ ролі, включно з
    viewer — `SELF_SERVICE_PATHS` (task-79) відкриває мутацію тут навіть
    там, де загальне правило вимагало б `member`."""
    email, password = make_user("read@yovth.com", role="viewer")
    headers = sign_in(email, password)

    created = auth_client.post(
        "/api/v1/me/tokens",
        json={"label": "my-script", "role": "member", "expires_in_days": 30},
        headers=headers,
    )
    assert created.status_code == 201
    # Стеля: viewer попросив member, отримав viewer (§3.3).
    assert created.json()["role"] == "viewer"

    token_id = created.json()["id"]
    revoked = auth_client.delete(f"/api/v1/me/tokens/{token_id}", headers=headers)
    assert revoked.status_code == 204


def test_admin_can_issue_a_lesser_viewer_token_for_scripts(
    auth_client: TestClient, make_user, sign_in
) -> None:
    """§3.3: видати собі МЕНШЕ дозволено й заохочено."""
    email, password = make_user("boss@yovth.com", role="admin")
    headers = sign_in(email, password)

    created = auth_client.post(
        "/api/v1/me/tokens",
        json={"label": "read-only-script", "role": "viewer", "expires_in_days": 30},
        headers=headers,
    )
    assert created.status_code == 201
    assert created.json()["role"] == "viewer"


def test_effective_role_follows_a_live_demotion(
    auth_client: TestClient, make_user, sign_in
) -> None:
    """Демонстрація вживу (§3.1 плану): admin-токен, виданий на роль
    "admin", того ж запиту видає лише "viewer" у СВОЇХ похідних дій, щойно
    адмін понижає власника — без нового відкликання, той самий сирий токен."""
    from sqlalchemy.orm import sessionmaker

    from app.db import session as db_session
    from app.db.repositories.users import UserRepository
    from app.schemas.enums import UserRole

    email, password = make_user("ivan@yovth.com", role="admin")
    headers = sign_in(email, password)
    token = auth_client.post(
        "/api/v1/me/tokens",
        json={"label": "ci", "role": "admin", "expires_in_days": 90},
        headers=headers,
    ).json()["token"]
    bearer = {"Authorization": f"Bearer {token}"}

    # Поки власник admin: цей самий токен може створити собі admin-токен.
    while_admin = auth_client.post(
        "/api/v1/me/tokens", json={"label": "while-admin"}, headers=bearer
    )
    assert while_admin.status_code == 201
    assert while_admin.json()["role"] == "member"  # дефолт запиту, не питали admin

    maker = sessionmaker(bind=db_session.get_sync_engine(), expire_on_commit=False)
    with maker() as session:
        repo = UserRepository(session)
        user = repo.get_by_email(email)
        repo.update_profile(user, role=UserRole.viewer)
        session.commit()

    # Той самий сирий токен, БЕЗ повторного логіну чи нового bearer'а:
    # запит на member тепер обрізається до viewer, бо власника понижено.
    after_demotion = auth_client.post(
        "/api/v1/me/tokens", json={"label": "after-demotion", "role": "member"}, headers=bearer
    )
    assert after_demotion.status_code == 201
    assert after_demotion.json()["role"] == "viewer", (
        "учорашній admin-токен видає сьогодні лише viewer-похідні"
    )


def test_cannot_revoke_someone_elses_token(auth_client: TestClient, make_user, sign_in) -> None:
    # `auth_client` has ONE shared cookie jar — signing in as the second
    # user overwrites the first user's session cookie. Each mutating call
    # happens immediately after its own sign-in, before the other logs in.
    alice_email, alice_password = make_user("alice@yovth.com")
    bob_email, bob_password = make_user("bob@yovth.com")

    alice_headers = sign_in(alice_email, alice_password)
    alice_token = auth_client.post(
        "/api/v1/me/tokens",
        json={"label": "alice-ci", "role": "member", "expires_in_days": 90},
        headers=alice_headers,
    ).json()

    bob_headers = sign_in(bob_email, bob_password)
    response = auth_client.delete(
        f"/api/v1/me/tokens/{alice_token['id']}", headers=bob_headers
    )
    assert response.status_code == 404

    # Alice's token is untouched — sign back in as her to check.
    alice_headers = sign_in(alice_email, alice_password)
    listing = auth_client.get("/api/v1/me/tokens", headers=alice_headers).json()
    assert listing[0]["revoked_at"] is None


def test_journal_shows_the_real_person_behind_the_token(
    auth_client: TestClient, make_user, sign_in, audit_events
) -> None:
    """GET isn't audited (task-72: only mutations + auth.* + denials) —
    so this proves the point through a SECOND mutation made WITH the
    bearer token, whose journal row must show the real owner, not "agent"."""
    email, password = make_user("ivan@yovth.com", role="admin")
    headers = sign_in(email, password)
    token = auth_client.post(
        "/api/v1/me/tokens",
        json={"label": "claude-code", "role": "member", "expires_in_days": 90},
        headers=headers,
    ).json()["token"]

    auth_client.post(
        "/api/v1/me/tokens",
        json={"label": "made-via-bearer"},
        headers={"Authorization": f"Bearer {token}"},
    )

    events = audit_events(action="me.token.create")
    matching = [e for e in events if e.details_json.get("token_label") == "claude-code"]
    assert matching, "the bearer-authenticated mutation carries its token's label"
    assert matching[0].actor_email == email
    assert matching[0].actor_id != "agent"


def test_no_secret_value_leaks_into_the_response_text_besides_the_create_call(
    auth_client: TestClient, make_user, sign_in
) -> None:
    email, password = make_user("ivan@yovth.com")
    headers = sign_in(email, password)
    created = auth_client.post(
        "/api/v1/me/tokens",
        json={"label": "ci", "role": "member", "expires_in_days": 90},
        headers=headers,
    )
    token = created.json()["token"]

    for _ in range(2):
        response = auth_client.get("/api/v1/me/tokens", headers=headers)
        assert token not in response.text
