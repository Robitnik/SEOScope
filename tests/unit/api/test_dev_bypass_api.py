"""Dev-only доступ без входу крізь справжній HTTP-стек (task-73).

Окремий модуль, а не доповнення `test_auth_api.py`: тут потрібен власний
`TestClient(client=("127.0.0.1", ...))`, якого решта файлу не використовує.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.db import session as db_session

pytestmark = pytest.mark.auth


def _loopback_client(tmp_path, monkeypatch, **env: str) -> TestClient:
    """Той самий стек, що `auth_client` (`tests/unit/api/conftest.py`), але
    з адресою джерела `127.0.0.1` і без обов'язкового логіну — саме це тут
    перевіряється."""
    from sqlalchemy.orm import sessionmaker

    from app.audit.sink import reset_audit_sink_for_tests
    from app.db.base import Base

    db_path = tmp_path / "test.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
    monkeypatch.setenv("ARTIFACTS_DIR", str(tmp_path / "artifacts"))
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("AUTH_COOKIE_SECURE", "false")
    monkeypatch.setenv("AUDIT_FALLBACK_PATH", str(tmp_path / "audit_fallback.jsonl"))
    # Явні безпечні дефолти для ОБОХ task-73-прапорців — інакше цей тест
    # читав би РЕАЛЬНИЙ `.env` розробника (у ньому вони свідомо увімкнені
    # для живої роботи, task-73) замість власного стану, який тестує.
    monkeypatch.setenv("AUTH_LOCALHOST_BYPASS", env.pop("AUTH_LOCALHOST_BYPASS", "false"))
    monkeypatch.setenv("AGENT_DEV_TOKEN", env.pop("AGENT_DEV_TOKEN", ""))
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    db_session._async_engine = None
    db_session._async_sessionmaker = None
    db_session._sync_engine = None
    db_session._sync_sessionmaker = None
    get_settings.cache_clear()
    reset_audit_sink_for_tests()

    Base.metadata.create_all(db_session.get_sync_engine())
    sessionmaker(bind=db_session.get_sync_engine(), expire_on_commit=False)

    from app.main import app

    return TestClient(app, client=("127.0.0.1", 54321))


def test_localhost_bypass_is_off_by_default(tmp_path, monkeypatch) -> None:
    """Найважливіший тест ланцюга: без явного `AUTH_LOCALHOST_BYPASS=true`
    localhost поводиться як будь-яка інша адреса — 401/303, як для всіх."""
    with _loopback_client(tmp_path, monkeypatch) as client:
        assert client.get("/api/v1/dashboard").status_code == 401


def test_localhost_bypass_authenticates_every_request(tmp_path, monkeypatch) -> None:
    with _loopback_client(tmp_path, monkeypatch, AUTH_LOCALHOST_BYPASS="true") as client:
        session = client.get("/api/v1/auth/session").json()
        assert session["authenticated"] is True
        assert session["user"]["role"] == "admin"
        assert client.get("/api/v1/dashboard").status_code == 200


def test_localhost_bypass_mutation_needs_no_csrf_header(tmp_path, monkeypatch) -> None:
    with _loopback_client(tmp_path, monkeypatch, AUTH_LOCALHOST_BYPASS="true") as client:
        response = client.post(
            "/api/v1/admin/users",
            json={"email": "new@yovth.com", "name": "New", "role": "member"},
        )
        assert response.status_code == 201, response.text


def test_localhost_bypass_refuses_a_tunnel_shaped_request(tmp_path, monkeypatch) -> None:
    """`network_mode: host` означає, що Cloudflare-тунель форвардить на
    цю саму адресу `127.0.0.1` — цей тест і є доказом, що це не відкриває
    прод разом із оператором."""
    with _loopback_client(tmp_path, monkeypatch, AUTH_LOCALHOST_BYPASS="true") as client:
        response = client.get(
            "/api/v1/dashboard", headers={"Cf-Connecting-Ip": "203.0.113.7"}
        )
        assert response.status_code == 401


def test_localhost_bypass_dead_in_production(tmp_path, monkeypatch) -> None:
    with _loopback_client(
        tmp_path, monkeypatch, AUTH_LOCALHOST_BYPASS="true", ENVIRONMENT="production"
    ) as client:
        assert client.get("/api/v1/dashboard").status_code == 401


def test_agent_token_authenticates_without_cookies(tmp_path, monkeypatch) -> None:
    with _loopback_client(tmp_path, monkeypatch, AGENT_DEV_TOKEN="s3cr3t") as client:
        headers = {"Authorization": "Bearer s3cr3t"}
        session = client.get("/api/v1/auth/session", headers=headers).json()
        assert session["authenticated"] is True
        assert session["user"]["name"] == "dev-agent"
        assert client.get("/api/v1/dashboard", headers=headers).status_code == 200


def test_agent_token_respects_configured_role(tmp_path, monkeypatch) -> None:
    with _loopback_client(
        tmp_path, monkeypatch, AGENT_DEV_TOKEN="s3cr3t", AGENT_DEV_TOKEN_ROLE="viewer"
    ) as client:
        headers = {"Authorization": "Bearer s3cr3t"}
        assert client.get("/api/v1/dashboard", headers=headers).status_code == 200
        assert client.post("/api/v1/jobs", json={}, headers=headers).status_code == 403


def test_agent_token_dead_in_production(tmp_path, monkeypatch) -> None:
    with _loopback_client(
        tmp_path, monkeypatch, AGENT_DEV_TOKEN="s3cr3t", ENVIRONMENT="production"
    ) as client:
        headers = {"Authorization": "Bearer s3cr3t"}
        assert client.get("/api/v1/dashboard", headers=headers).status_code == 401


def test_agent_actions_are_labeled_in_the_audit_log(tmp_path, monkeypatch) -> None:
    from sqlalchemy.orm import sessionmaker

    from app.audit.sink import get_audit_sink
    from app.db.repositories.audit import AuditRepository

    with _loopback_client(tmp_path, monkeypatch, AGENT_DEV_TOKEN="s3cr3t") as client:
        client.post(
            "/api/v1/admin/users",
            json={"email": "seen@yovth.com", "name": "Seen", "role": "member"},
            headers={"Authorization": "Bearer s3cr3t"},
        )
        get_audit_sink().flush()
        maker = sessionmaker(bind=db_session.get_sync_engine(), expire_on_commit=False)
        with maker() as session:
            events = AuditRepository(session).query(action="admin.user.create", limit=5)
        assert any(event.actor_kind == "agent" for event in events)
