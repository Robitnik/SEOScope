"""Логін, сесія, CSRF, ролі (task-71) — крізь справжній HTTP-стек."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.auth


def test_anonymous_api_call_is_401(auth_client: TestClient) -> None:
    response = auth_client.get("/api/v1/dashboard")
    assert response.status_code == 401
    assert response.json()["detail"]["error_code"] == "auth_required"


def test_anonymous_navigation_is_redirected_to_the_login_page(auth_client: TestClient) -> None:
    response = auth_client.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login.html?next=")


def test_login_page_and_its_assets_stay_reachable(auth_client: TestClient) -> None:
    for path in ("/login.html", "/css/login.css", "/js/login.js", "/healthz"):
        assert auth_client.get(path).status_code == 200, path


def test_login_sets_an_httponly_session_cookie(auth_client: TestClient, make_user) -> None:
    email, password = make_user("ivan@yovth.com")
    response = auth_client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200
    cookie = response.headers["set-cookie"]
    assert "yovth_session=" in cookie
    assert "HttpOnly" in cookie
    assert "SameSite=lax" in cookie.lower() or "samesite=lax" in cookie.lower()
    # Пароль не повертається жодним полем відповіді.
    assert password not in response.text


def test_unknown_email_and_wrong_password_are_indistinguishable(
    auth_client: TestClient, make_user
) -> None:
    """Різні відповіді зробили б форму логіну довідником співробітників."""
    email, _ = make_user("ivan@yovth.com")
    wrong = auth_client.post("/api/v1/auth/login", json={"email": email, "password": "nope"})
    unknown = auth_client.post(
        "/api/v1/auth/login", json={"email": "ghost@yovth.com", "password": "nope"}
    )
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json() == unknown.json()


def test_account_locks_after_repeated_failures(auth_client: TestClient, make_user) -> None:
    email, password = make_user("ivan@yovth.com")
    for _ in range(10):
        auth_client.post("/api/v1/auth/login", json={"email": email, "password": "nope"})
    # Правильний пароль теж не пускає, поки триває блокування.
    blocked = auth_client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert blocked.status_code == 401


def test_signed_in_user_reaches_the_api(auth_client: TestClient, make_user, sign_in) -> None:
    email, password = make_user("ivan@yovth.com")
    sign_in(email, password)
    assert auth_client.get("/api/v1/dashboard").status_code == 200


def test_session_endpoint_reports_the_current_user(
    auth_client: TestClient, make_user, sign_in
) -> None:
    assert auth_client.get("/api/v1/auth/session").json()["authenticated"] is False
    email, password = make_user("ivan@yovth.com", role="admin")
    sign_in(email, password)
    payload = auth_client.get("/api/v1/auth/session").json()
    assert payload["authenticated"] is True
    assert payload["user"]["email"] == email
    assert payload["user"]["role"] == "admin"


def test_logout_kills_the_session(auth_client: TestClient, make_user, sign_in) -> None:
    email, password = make_user("ivan@yovth.com")
    headers = sign_in(email, password)
    assert auth_client.post("/api/v1/auth/logout", headers=headers).status_code == 204
    assert auth_client.get("/api/v1/dashboard").status_code == 401


def test_mutation_without_a_csrf_token_is_refused(
    auth_client: TestClient, make_user, sign_in
) -> None:
    email, password = make_user("ivan@yovth.com", role="admin")
    sign_in(email, password)
    response = auth_client.post(
        "/api/v1/admin/users", json={"email": "new@yovth.com", "name": "New"}
    )
    assert response.status_code == 403
    assert response.json()["detail"]["error_code"] == "csrf_failed"


def test_mutation_with_a_forged_csrf_token_is_refused(
    auth_client: TestClient, make_user, sign_in
) -> None:
    email, password = make_user("ivan@yovth.com", role="admin")
    sign_in(email, password)
    response = auth_client.post(
        "/api/v1/admin/users",
        json={"email": "new@yovth.com", "name": "New"},
        headers={"X-CSRF-Token": "forged"},
    )
    assert response.status_code == 403


def test_viewer_can_read_but_never_write(auth_client: TestClient, make_user, sign_in) -> None:
    email, password = make_user("read@yovth.com", role="viewer")
    headers = sign_in(email, password)
    assert auth_client.get("/api/v1/dashboard").status_code == 200
    response = auth_client.post("/api/v1/jobs", json={}, headers=headers)
    assert response.status_code == 403
    assert response.json()["detail"]["error_code"] == "forbidden"


def test_member_cannot_reach_admin_or_start_batches(
    auth_client: TestClient, make_user, sign_in
) -> None:
    email, password = make_user("member@yovth.com", role="member")
    headers = sign_in(email, password)
    assert auth_client.get("/api/v1/admin/users").status_code == 403
    # ЗАПУСК батчу — свідомо admin-only: один запуск палить зовнішні квоти по
    # всьому портфелю (рішення оператора, task-71 §2). Те саме для дій над
    # уже запущеним батчем: cancel/retry керують чужим портфельним прогоном.
    batch = "/api/v1/reports/batches"
    assert auth_client.post(batch, json={}, headers=headers).status_code == 403
    assert auth_client.post(f"{batch}/probe/cancel", json={}, headers=headers).status_code == 403
    retry = f"{batch}/probe/clients/probe/retry"
    assert auth_client.post(retry, json={}, headers=headers).status_code == 403


def test_every_role_can_read_batch_reports(auth_client: TestClient, make_user, sign_in) -> None:
    """Дзеркало тесту вище: заборонений лише запуск, не результат.

    Звіт, який вийшов із батчу, — робочий артефакт усієї агенції. Поки
    `GET /reports/batches` теж був admin-only, member/viewer отримували 403
    не лише на батч, а й на всю сторінку Reports: вона тягне історію одним
    `Promise.all([runs, batches])`, і 403 в одній гілці валив увесь екран.
    """
    for role in ("member", "viewer"):
        email, password = make_user(f"reader-{role}@yovth.com", role=role)
        sign_in(email, password)
        assert auth_client.get("/api/v1/reports/batches").status_code == 200
        # Неіснуючий id має дійти до хендлера (404), а не спинитись на ролі.
        detail = auth_client.get("/api/v1/reports/batches/8d1f4c2e-0000-4000-8000-000000000000")
        assert detail.status_code == 404, detail.text


def test_secrets_never_appear_in_logs(
    auth_client: TestClient, make_user, sign_in, caplog
) -> None:
    email, password = make_user("ivan@yovth.com", password="super-secret-pw")
    with caplog.at_level("INFO"):
        sign_in(email, password)
    assert "super-secret-pw" not in caplog.text
