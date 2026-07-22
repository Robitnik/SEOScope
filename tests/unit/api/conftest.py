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
