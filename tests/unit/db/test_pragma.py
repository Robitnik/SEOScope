"""PRAGMAs applied to every SQLite connection (PLAN §5.1/§6).

`busy_timeout` lowered from the original 30_000 to `Settings`'s own
default (task-66 §1.3, live-measured 2026-09-05): 30s was an amplifier of
the lock storm it was meant to protect against, not a safety net — see
`Settings.sqlite_busy_timeout_ms`'s own docstring, and
`tests/unit/db/test_session_pragmas.py` for the rest of that task's own
PRAGMA coverage.

task-75 §9: this is inherently SQLite-only (Postgres has no `PRAGMA`
syntax at all), so a local `engine` fixture shadows the shared
Postgres-backed one from `tests/unit/db/conftest.py` for this file only.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from app.core.config import Settings
from app.db.session import build_sync_engine


@pytest.fixture
def engine(tmp_path):
    settings = Settings(_env_file=None, database_url=f"sqlite:///{tmp_path / 'test.db'}")
    eng = build_sync_engine(settings)
    yield eng
    eng.dispose()


def test_pragmas_applied_on_connect(engine):
    with engine.connect() as conn:
        assert conn.execute(text("PRAGMA journal_mode")).scalar() == "wal"
        assert conn.execute(text("PRAGMA foreign_keys")).scalar() == 1
        assert conn.execute(text("PRAGMA busy_timeout")).scalar() == 5000  # Settings default
