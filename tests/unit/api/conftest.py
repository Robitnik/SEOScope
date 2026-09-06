from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.db import models  # noqa: F401 - registers tables on Base.metadata
from app.db import session as db_session
from app.db.base import Base
from app.integrations import registry


def _reset_engines() -> None:
    db_session._async_engine = None
    db_session._async_sessionmaker = None
    db_session._sync_engine = None
    db_session._sync_sessionmaker = None
    get_settings.cache_clear()


@pytest.fixture
def client(tmp_path, monkeypatch) -> Iterator[TestClient]:
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
    monkeypatch.setenv("ARTIFACTS_DIR", str(tmp_path / "artifacts"))
    _reset_engines()

    Base.metadata.create_all(db_session.get_sync_engine())
    monkeypatch.setattr(registry, "_registry", {})

    from app.main import app

    with TestClient(app) as test_client:
        yield test_client

    _reset_engines()


@pytest.fixture
def db(client: TestClient):
    """A sync `Session` on the same temp DB the `client` fixture wired up —
    for seeding fixtures directly through repositories instead of the API.
    """
    from sqlalchemy.orm import sessionmaker

    maker = sessionmaker(bind=db_session.get_sync_engine(), expire_on_commit=False)
    with maker() as session:
        yield session
        session.commit()


@pytest.fixture
def auth_client(tmp_path, monkeypatch) -> Iterator[TestClient]:
    """`TestClient` із УВІМКНЕНИМ гейтом (task-71) — для тестів самої авторизації.

    Окрема фікстура, а не прапорець у `client`: решта набору свідомо працює з
    вимкненим гейтом (див. кореневий conftest), і змішувати два режими в одній
    фікстурі означало б, що кожен існуючий тест мусив би знати про логін.
    """
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
    monkeypatch.setenv("ARTIFACTS_DIR", str(tmp_path / "artifacts"))
    monkeypatch.setenv("AUTH_ENABLED", "true")
    # Тест-клієнт ходить по http://testserver — cookie з `Secure` браузерна
    # реалізація в httpx не віддала б назад.
    monkeypatch.setenv("AUTH_COOKIE_SECURE", "false")
    _reset_engines()

    Base.metadata.create_all(db_session.get_sync_engine())
    monkeypatch.setattr(registry, "_registry", {})

    from app.main import app

    with TestClient(app) as test_client:
        yield test_client

    _reset_engines()


@pytest.fixture
def make_user(auth_client: TestClient):
    """Створює обліковий запис прямо через репозиторій і повертає `(email, password)`."""
    from sqlalchemy.orm import sessionmaker

    from app.db.repositories.users import UserRepository
    from app.schemas.enums import UserRole

    maker = sessionmaker(bind=db_session.get_sync_engine(), expire_on_commit=False)

    def _make(
        email: str, role: str = "member", password: str = "test-password-1"
    ) -> tuple[str, str]:
        with maker() as session:
            UserRepository(session).create(
                email=email, name=email.split("@")[0], role=UserRole(role), password=password
            )
            session.commit()
        return email, password

    return _make


@pytest.fixture
def sign_in(auth_client: TestClient):
    """Логінить і повертає заголовки з CSRF — усі мутуючі виклики їх вимагають."""

    def _sign_in(email: str, password: str) -> dict[str, str]:
        response = auth_client.post(
            "/api/v1/auth/login", json={"email": email, "password": password}
        )
        assert response.status_code == 200, response.text
        return {"X-CSRF-Token": response.json()["csrf_token"]}

    return _sign_in
