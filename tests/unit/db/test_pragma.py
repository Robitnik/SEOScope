"""PRAGMAs applied to every SQLite connection (PLAN §5.1/§6)."""

from __future__ import annotations

from sqlalchemy import text


def test_pragmas_applied_on_connect(engine):
    with engine.connect() as conn:
        assert conn.execute(text("PRAGMA journal_mode")).scalar() == "wal"
        assert conn.execute(text("PRAGMA foreign_keys")).scalar() == 1
        assert conn.execute(text("PRAGMA busy_timeout")).scalar() == 30_000
