from __future__ import annotations

import pytest

from app.reports.ninja_api import reset_rate_limiter_for_tests


@pytest.fixture(autouse=True)
def _fresh_rate_limiter():
    """The token bucket in `app.reports.ninja_api` is a module-level
    singleton by design (task-47 §2.3: the limit is on the ACCOUNT, shared
    across every concurrent `ReportingNinjaClient`) — without a reset, one
    test's requests would silently deplete the next test's tokens. A high
    rate keeps every test that does not specifically exercise the limiter
    itself running at full speed.
    """
    reset_rate_limiter_for_tests(1_000_000.0)
    yield
    reset_rate_limiter_for_tests(None)


@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    """Points `app.cli`'s process-global sync engine at an isolated,
    schema-migrated temp DB for the duration of one test — same shape as
    `tests/unit/importers/conftest.py`'s own fixture (task-07), duplicated
    locally rather than shared across directories, since fixtures are not
    automatically visible outside the conftest that defines them.
    """
    from app.core.config import get_settings
    from app.db import models  # noqa: F401 - registers tables on Base.metadata
    from app.db import session as db_session
    from app.db.base import Base

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
