"""`AuditMiddleware` крізь справжній HTTP-стек (task-72)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.auth


def test_login_is_recorded_with_the_email_and_no_password(
    auth_client: TestClient, make_user, audit_events
) -> None:
    email, password = make_user("ivan@yovth.com", password="super-secret-pw")
    auth_client.post("/api/v1/auth/login", json={"email": email, "password": password})

    events = audit_events(action="auth.login")
    assert len(events) == 1
    assert events[0].actor_email == email
    assert events[0].outcome == "success"
    assert "super-secret-pw" not in str(events[0].details_json)


def test_failed_login_is_recorded_as_denied(
    auth_client: TestClient, make_user, audit_events
) -> None:
    email, _ = make_user("ivan@yovth.com")
    auth_client.post("/api/v1/auth/login", json={"email": email, "password": "wrong"})

    events = audit_events(action="auth.login", outcome="denied")
    assert len(events) == 1
    assert events[0].details_json.get("request", {}).get("email") == email


def test_plain_dashboard_reads_are_not_logged(
    auth_client: TestClient, make_user, sign_in, audit_events
) -> None:
    email, password = make_user("ivan@yovth.com")
    sign_in(email, password)
    auth_client.get("/api/v1/dashboard")
    auth_client.get("/api/v1/dashboard")

    events = audit_events()
    assert all(e.path != "/api/v1/dashboard" for e in events), (
        "звичайні GET-читання не мають потрапляти в журнал — на один рендер їх десятки"
    )


def test_mutation_is_recorded_with_the_actor_and_readable_action(
    auth_client: TestClient, make_user, sign_in, audit_events
) -> None:
    email, password = make_user("boss@yovth.com", role="admin")
    headers = sign_in(email, password)
    auth_client.post(
        "/api/v1/admin/users",
        json={"email": "new@yovth.com", "name": "New", "role": "member"},
        headers=headers,
    )

    events = audit_events(action="admin.user.create")
    assert len(events) == 1
    event = events[0]
    assert event.actor_email == email
    assert event.actor_role == "admin"
    assert event.outcome == "success"
    assert event.status_code == 201
    assert event.details_json["request"]["email"] == "new@yovth.com"


def test_forbidden_action_is_recorded_as_denied(
    auth_client: TestClient, make_user, sign_in, audit_events
) -> None:
    email, password = make_user("read@yovth.com", role="viewer")
    headers = sign_in(email, password)
    auth_client.post("/api/v1/jobs", json={}, headers=headers)

    events = audit_events(outcome="denied")
    assert any(e.actor_email == email and e.status_code == 403 for e in events)


def test_generated_password_never_reaches_the_audit_log(
    auth_client: TestClient, make_user, sign_in, audit_events
) -> None:
    email, password = make_user("boss@yovth.com", role="admin")
    headers = sign_in(email, password)
    created = auth_client.post(
        "/api/v1/admin/users",
        json={"email": "fresh@yovth.com", "name": "Fresh", "role": "member"},
        headers=headers,
    ).json()

    events = audit_events()
    assert all(created["password"] not in str(e.details_json) for e in events)


def test_denied_read_is_recorded_but_not_every_401_on_get(
    auth_client: TestClient, audit_events
) -> None:
    """403 на будь-якому методі журналюється; голий 401 на GET (протухла
    сесія випадкового читання) — ні, інакше одна вкладка з мертвою сесією
    засмічує журнал десятками однакових рядків за один рендер."""
    auth_client.get("/api/v1/dashboard")
    events = audit_events()
    assert events == []
