"""Адмінка користувачів (task-71): видача акаунтів, ролі, захист від самоблоку."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.auth


@pytest.fixture
def admin(auth_client: TestClient, make_user, sign_in) -> dict[str, str]:
    email, password = make_user("boss@yovth.com", role="admin")
    return sign_in(email, password)


def test_create_returns_the_generated_password_exactly_once(
    auth_client: TestClient, admin
) -> None:
    created = auth_client.post(
        "/api/v1/admin/users",
        json={"email": "New.Person@Yovth.com", "name": "New Person", "role": "member"},
        headers=admin,
    )
    assert created.status_code == 201
    payload = created.json()
    password = payload["password"]
    assert len(password.split("-")) == 4
    # Канонізація адреси: адмін набрав із великих літер, зберігся lowercase.
    assert payload["user"]["email"] == "new.person@yovth.com"

    # Пароль не витікає у жодному наступному читанні.
    listing = auth_client.get("/api/v1/admin/users", headers=admin)
    assert password not in listing.text


def test_generated_password_actually_works(auth_client: TestClient, admin) -> None:
    created = auth_client.post(
        "/api/v1/admin/users",
        json={"email": "new@yovth.com", "name": "New", "role": "member"},
        headers=admin,
    ).json()
    auth_client.post("/api/v1/auth/logout", headers=admin)
    response = auth_client.post(
        "/api/v1/auth/login", json={"email": "new@yovth.com", "password": created["password"]}
    )
    assert response.status_code == 200


def test_duplicate_email_is_a_clean_conflict(auth_client: TestClient, admin) -> None:
    body = {"email": "dupe@yovth.com", "name": "Dupe", "role": "member"}
    assert auth_client.post("/api/v1/admin/users", json=body, headers=admin).status_code == 201
    second = auth_client.post("/api/v1/admin/users", json=body, headers=admin)
    assert second.status_code == 409
    assert second.json()["detail"]["error_code"] == "email_taken"


def test_reset_password_issues_a_new_one_and_signs_the_user_out(
    auth_client: TestClient, admin, make_user, sign_in
) -> None:
    email, password = make_user("ivan@yovth.com")
    users = auth_client.get("/api/v1/admin/users", headers=admin).json()
    target = next(user for user in users if user["email"] == email)

    reset = auth_client.post(
        f"/api/v1/admin/users/{target['id']}/reset-password", headers=admin
    )
    assert reset.status_code == 200
    assert reset.json()["password"] != password

    # Старий пароль більше не працює, новий — працює.
    auth_client.post("/api/v1/auth/logout", headers=admin)
    assert (
        auth_client.post(
            "/api/v1/auth/login", json={"email": email, "password": password}
        ).status_code
        == 401
    )
    assert (
        auth_client.post(
            "/api/v1/auth/login", json={"email": email, "password": reset.json()["password"]}
        ).status_code
        == 200
    )


def test_deactivation_ends_live_sessions_immediately(
    auth_client: TestClient, admin, make_user
) -> None:
    email, password = make_user("ivan@yovth.com")
    users = auth_client.get("/api/v1/admin/users", headers=admin).json()
    target = next(user for user in users if user["email"] == email)

    # Окремий клієнт-«браузер» цієї людини, щоб не збивати cookie адміна.
    with TestClient(auth_client.app) as other:
        login = other.post("/api/v1/auth/login", json={"email": email, "password": password})
        assert other.get("/api/v1/dashboard").status_code == 200

        auth_client.patch(
            f"/api/v1/admin/users/{target['id']}", json={"is_active": False}, headers=admin
        )
        assert other.get("/api/v1/dashboard").status_code == 401
        assert login.status_code == 200


def test_admin_cannot_demote_themselves(auth_client: TestClient, admin) -> None:
    me = auth_client.get("/api/v1/auth/session", headers=admin).json()["user"]
    response = auth_client.patch(
        f"/api/v1/admin/users/{me['id']}", json={"role": "member"}, headers=admin
    )
    assert response.status_code == 409
    assert response.json()["detail"]["error_code"] == "self_demotion"


def test_the_last_admin_cannot_be_removed(auth_client: TestClient, admin, make_user) -> None:
    second_email, second_password = make_user("second@yovth.com", role="admin")
    users = auth_client.get("/api/v1/admin/users", headers=admin).json()
    me = auth_client.get("/api/v1/auth/session", headers=admin).json()["user"]
    other = next(user for user in users if user["email"] == second_email)

    # Другого адміна зняти можна…
    assert (
        auth_client.patch(
            f"/api/v1/admin/users/{other['id']}", json={"role": "member"}, headers=admin
        ).status_code
        == 200
    )
    # …а себе — ні, інакше система лишиться без нікого, хто заводить людей.
    assert (
        auth_client.patch(
            f"/api/v1/admin/users/{me['id']}", json={"is_active": False}, headers=admin
        ).status_code
        == 409
    )


def test_sessions_listing_marks_the_current_session(auth_client: TestClient, admin) -> None:
    me = auth_client.get("/api/v1/auth/session", headers=admin).json()["user"]
    sessions = auth_client.get(f"/api/v1/admin/users/{me['id']}/sessions", headers=admin).json()
    assert len(sessions) == 1
    assert sessions[0]["current"] is True


def test_missing_user_is_a_clean_404(auth_client: TestClient, admin) -> None:
    assert (
        auth_client.post("/api/v1/admin/users/nope/reset-password", headers=admin).status_code
        == 404
    )
