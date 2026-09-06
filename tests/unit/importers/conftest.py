from __future__ import annotations

import pytest
from harness import build_test_engine, test_database_url
from sqlalchemy.orm import Session, sessionmaker


@pytest.fixture
def engine():
    eng = build_test_engine()
    yield eng
    eng.dispose()


@pytest.fixture
def session_factory(engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    """Points `app.cli`'s process-global sync engine at the shared
    isolated test database (task-75 §9: `yovth_test` on Postgres, not a
    per-test SQLite file) for the duration of one test.
    """
    from app.core.config import get_settings
    from app.db import session as db_session
    from app.db import writegate as db_writegate

    monkeypatch.setenv("DATABASE_URL", test_database_url())
    get_settings.cache_clear()
    db_session._sync_engine = None
    db_session._sync_sessionmaker = None
    # task-66: the write gate is ALSO a process-global singleton, resolved
    # from whatever `Settings` was cached at first use — reset it alongside
    # the engine/sessionmaker above so it derives its lock path from THIS
    # test's own tmp DB, not a stale previous one.
    db_writegate.reset_write_gate_for_tests()

    build_test_engine(get_settings())  # ensures schema exists, truncates for isolation
    yield tmp_path

    db_session._sync_engine = None
    db_session._sync_sessionmaker = None
    db_writegate.reset_write_gate_for_tests()
    get_settings.cache_clear()
