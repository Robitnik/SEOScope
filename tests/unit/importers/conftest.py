from __future__ import annotations

import pytest
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.db import models  # noqa: F401 - registers tables on Base.metadata
from app.db.base import Base
from app.db.session import build_sync_engine


@pytest.fixture
def engine(tmp_path):
    db_path = tmp_path / "test.db"
    settings = Settings(_env_file=None, database_url=f"sqlite:///{db_path}")
    eng = build_sync_engine(settings)
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def session_factory(engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    """Points `app.cli`'s process-global sync engine at an isolated,
    schema-migrated temp DB for the duration of one test.
    """
    from app.core.config import get_settings
    from app.db import session as db_session

    db_path = tmp_path / "cli_test.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
    get_settings.cache_clear()
    db_session._sync_engine = None
    db_session._sync_sessionmaker = None

    Base.metadata.create_all(db_session.get_sync_engine())
    yield tmp_path

    db_session._sync_engine = None
    db_session._sync_sessionmaker = None
    get_settings.cache_clear()
