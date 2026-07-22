"""DB + migration readiness check consumed by `GET /readyz` (task-01's hook).

Never raises: a readiness probe that 500s on a DB outage is worse than one
that cleanly reports `not_ready`, since the outage is exactly the condition
it exists to surface.
"""

from __future__ import annotations

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.db.session import get_async_engine

_ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"


async def database_ready(engine: AsyncEngine | None = None) -> tuple[bool, str]:
    try:
        engine = engine or get_async_engine()
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
            result = await conn.execute(text("SELECT version_num FROM alembic_version"))
            row = result.first()
            current = row[0] if row else None
    except Exception as exc:
        return False, f"database_unreachable: {exc.__class__.__name__}"

    try:
        config = Config(str(_ALEMBIC_INI))
        script = ScriptDirectory.from_config(config)
        head = script.get_current_head()
    except Exception as exc:
        return False, f"migration_script_error: {exc.__class__.__name__}"

    if current != head:
        return False, f"schema_outdated: current={current} head={head}"
    return True, "ok"
