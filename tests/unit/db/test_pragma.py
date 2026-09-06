"""PRAGMAs applied to every SQLite connection (PLAN §5.1/§6).

`busy_timeout` lowered from the original 30_000 to the `engine` fixture's
own `Settings` default (task-66 §1.3, live-measured 2026-09-05): 30s was an
amplifier of the lock storm it was meant to protect against, not a safety
net — see `Settings.sqlite_busy_timeout_ms`'s own docstring, and
`tests/unit/db/test_session_pragmas.py` for the rest of that task's own
PRAGMA coverage.
"""

from __future__ import annotations

from sqlalchemy import text


def test_pragmas_applied_on_connect(engine):
    with engine.connect() as conn:
        assert conn.execute(text("PRAGMA journal_mode")).scalar() == "wal"
        assert conn.execute(text("PRAGMA foreign_keys")).scalar() == 1
        assert conn.execute(text("PRAGMA busy_timeout")).scalar() == 5000  # Settings default
