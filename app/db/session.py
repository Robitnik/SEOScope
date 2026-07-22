"""Engine/session wiring (PLAN §5.1/§6).

The API is async (`sqlite+aiosqlite`); the worker is a plain sync process and
uses the `pysqlite` driver instead — both point at the same on-disk file and
both get the same PRAGMAs applied on every new DBAPI connection, so the two
runtimes never disagree about WAL/foreign_keys/busy_timeout.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Generator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import Engine, event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings, get_settings

# SQLite busy_timeout in ms: how long a writer waits on a lock before raising
# `database is locked`. Generous because the single worker holds only short
# transactions (PLAN §5.1), so contention is expected to be brief, not absent.
SQLITE_BUSY_TIMEOUT_MS = 30_000


def sqlite_sync_url(database_url: str) -> str:
    """`sqlite+aiosqlite:///path` -> `sqlite:///path` for the sync worker/Alembic engine."""
    return database_url.replace("sqlite+aiosqlite://", "sqlite://", 1)


def _apply_sqlite_pragma(dbapi_connection: Any, _connection_record: Any) -> None:
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
    finally:
        cursor.close()


def build_async_engine(settings: Settings | None = None) -> AsyncEngine:
    settings = settings or get_settings()
    engine = create_async_engine(settings.database_url)
    event.listen(engine.sync_engine, "connect", _apply_sqlite_pragma)
    return engine


def build_sync_engine(settings: Settings | None = None) -> Engine:
    settings = settings or get_settings()
    from sqlalchemy import create_engine

    engine = create_engine(sqlite_sync_url(settings.database_url))
    event.listen(engine, "connect", _apply_sqlite_pragma)
    return engine


_async_engine: AsyncEngine | None = None
_async_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def get_async_engine() -> AsyncEngine:
    global _async_engine
    if _async_engine is None:
        _async_engine = build_async_engine()
    return _async_engine


def get_async_sessionmaker() -> async_sessionmaker[AsyncSession]:
    global _async_sessionmaker
    if _async_sessionmaker is None:
        _async_sessionmaker = async_sessionmaker(
            bind=get_async_engine(), expire_on_commit=False
        )
    return _async_sessionmaker


async def get_db() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency: one short-lived session per request."""
    async with get_async_sessionmaker()() as session:
        yield session


_sync_engine: Engine | None = None
_sync_sessionmaker: sessionmaker[Session] | None = None


def get_sync_engine() -> Engine:
    global _sync_engine
    if _sync_engine is None:
        _sync_engine = build_sync_engine()
    return _sync_engine


def get_sync_sessionmaker() -> sessionmaker[Session]:
    global _sync_sessionmaker
    if _sync_sessionmaker is None:
        _sync_sessionmaker = sessionmaker(bind=get_sync_engine(), expire_on_commit=False)
    return _sync_sessionmaker


@contextmanager
def worker_session_scope() -> Generator[Session, None, None]:
    """Worker context manager: short transaction, commit on success, rollback on error."""
    session = get_sync_sessionmaker()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
