"""`database_ready()` backing `GET /readyz` (task-01 hook, filled in by task-03)."""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import get_settings
from app.db.health import database_ready

_REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.asyncio
async def test_not_ready_when_schema_missing(tmp_path):
    db_path = tmp_path / "empty.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    try:
        ready, detail = await database_ready(engine)
        assert ready is False
        assert "database_unreachable" in detail
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_ready_after_migration(tmp_path, monkeypatch):
    db_path = tmp_path / "migrated.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    get_settings.cache_clear()
    try:
        cfg = Config(str(_REPO_ROOT / "alembic.ini"))
        command.upgrade(cfg, "head")
    finally:
        get_settings.cache_clear()

    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    try:
        ready, detail = await database_ready(engine)
        assert ready is True
        assert detail == "ok"
    finally:
        await engine.dispose()
