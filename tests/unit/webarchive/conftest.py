from __future__ import annotations

import pytest
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.db import models  # noqa: F401 - registers tables on Base.metadata
from app.db.base import Base
from app.db.session import build_sync_engine
from app.webarchive.writequeue import ArchiveWriteController


@pytest.fixture
def db_settings(tmp_path) -> Settings:
    """The `Settings` object that actually determines which SQLite FILE
    `engine`/`session` talk to. Named `db_settings`, not `settings` —
    several test files in this package (`test_capture.py`) already define
    their OWN `settings` fixture for a different purpose (feature toggles,
    `artifacts_dir`) with no `database_url` override at all. A fixture
    named plain `settings` here would be SILENTLY SHADOWED by those files'
    own definition for every test in them, which would point `engine` at
    `Settings`'s default `database_url` — the real, bind-mounted
    `app_data/app.db` — instead of an isolated tmp file. Live-caught
    2026-09-06: exactly that happened once, before this fixture had its
    own name, and inserted a real 'Acme' test row into the production
    database (see task-69 completion report).
    """
    db_path = tmp_path / "test.db"
    return Settings(_env_file=None, database_url=f"sqlite:///{db_path}")


@pytest.fixture
def engine(db_settings):
    eng = build_sync_engine(db_settings)
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def session(engine) -> Session:
    maker = sessionmaker(bind=engine, expire_on_commit=False)
    with maker() as s:
        yield s


@pytest.fixture
def session_scope(engine):
    """A REAL second session-opener bound to the same file-backed engine as
    `session` (task-62) — not the same object as the `session` fixture, so
    tests that exercise cross-session freshness (the cancel-check, task-62
    §1.6) see genuinely independent connections, exactly like the worker's
    own session vs. the API's own session in production.
    """
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def write_controller():
    controller = ArchiveWriteController()
    controller.start()
    yield controller
    controller.stop()


@pytest.fixture
def artifacts_dir(tmp_path):
    path = tmp_path / "artifacts"
    path.mkdir()
    return path
