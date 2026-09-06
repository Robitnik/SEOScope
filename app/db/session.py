"""Engine/session wiring (PLAN §5.1/§6).

The API is async (`sqlite+aiosqlite`); the worker is a plain sync process and
uses the `pysqlite` driver instead — both point at the same on-disk file and
both get the same PRAGMAs applied on every new DBAPI connection, so the two
runtimes never disagree about WAL/foreign_keys/busy_timeout.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Generator
from contextlib import contextmanager
from functools import partial
from pathlib import Path
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

# Kept for backward compat with anything reading the old module-level
# constant directly — the actual value applied to connections now comes
# from `Settings.sqlite_busy_timeout_ms` (task-66 §1.3), deliberately
# lowered from this 30s default: see that setting's own docstring for why
# 30s was an amplifier of the lock-storm, not a safety net.
SQLITE_BUSY_TIMEOUT_MS = 30_000


def sqlite_sync_url(database_url: str) -> str:
    """`sqlite+aiosqlite:///path` -> `sqlite:///path` for the sync worker/Alembic engine."""
    return database_url.replace("sqlite+aiosqlite://", "sqlite://", 1)


def sqlite_db_path(settings: Settings) -> Path:
    """The actual SQLite file behind `database_url` — needed by anything
    that has to look at the file itself rather than talk SQL to it (the
    `-wal` sibling's size, for `DbWriteGate`'s idle checkpointer).
    """
    return Path(sqlite_sync_url(settings.database_url).split("sqlite:///", 1)[-1])


def default_write_lock_path(settings: Settings) -> Path:
    """Where `DbWriteGate` (task-66 §2.1) takes its cross-process flock:
    a `<db file>.write.lock` sibling of the actual SQLite file, derived
    from `database_url` rather than a fixed path — so each isolated test
    database (`tests/harness/db.py`'s own tmp file) gets its own lock
    instead of sharing the real deployment's, which is bind-mounted into
    the very same container these tests run in.
    """
    if settings.db_write_lock_path is not None:
        return settings.db_write_lock_path
    return Path(f"{sqlite_db_path(settings)}.write.lock")


def _apply_sqlite_pragma(
    dbapi_connection: Any, _connection_record: Any, *, settings: Settings
) -> None:
    """Every PRAGMA here (task-66 §1.1) is live-measured, not assumed:
    `synchronous=NORMAL` cut commit cost ~70x (1.42ms -> 0.02ms) on a copy
    of the production DB, paid INSIDE the held writer lock so it
    multiplies across every other writer queued behind it; `cache_size`/
    `mmap_size` made no measurable difference on a warm page cache
    (already covered by the OS) but are set anyway for a cold start and
    long-tail latency, not because they were this task's bottleneck.
    """
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute(f"PRAGMA synchronous={settings.sqlite_synchronous}")
        cursor.execute("PRAGMA temp_store=MEMORY")
        cursor.execute(f"PRAGMA cache_size=-{settings.sqlite_cache_kib}")
        cursor.execute(f"PRAGMA mmap_size={settings.sqlite_mmap_bytes}")
        cursor.execute(f"PRAGMA busy_timeout={settings.sqlite_busy_timeout_ms}")
        # task-67 §2: `0` takes the WAL checkpoint OUT of the write path —
        # measured ~4-5x on throughput and up to ~50x on the longest stall
        # another writer can see, because the checkpoint stops landing
        # inside whichever ordinary commit happened to overflow the WAL.
        # `DbWriteGate`'s idle checkpointer does that work instead. See
        # `Settings.sqlite_wal_autocheckpoint` for the numbers and for why
        # the checkpointer is mandatory rather than an optimisation.
        cursor.execute(f"PRAGMA wal_autocheckpoint={settings.sqlite_wal_autocheckpoint}")
    finally:
        cursor.close()


def build_async_engine(settings: Settings | None = None) -> AsyncEngine:
    settings = settings or get_settings()
    # task-66 §1.2 sized the sync engine's pool but left the async one on
    # SQLAlchemy's stock 5 + 10 overflow / 30s timeout — the half that
    # actually serves `/dashboard`, `/clients` and every other `async def`
    # endpoint. Same reasoning as the sync side: WAL allows unlimited
    # concurrent readers, so a small pool buys nothing and a 30s
    # `pool_timeout` turns a momentary pile-up into a hung browser tab.
    engine = create_async_engine(
        settings.database_url,
        pool_size=settings.sqlite_async_pool_size,
        max_overflow=settings.sqlite_async_pool_max_overflow,
        pool_timeout=settings.sqlite_pool_timeout_s,
    )
    # task-75: these PRAGMAs are SQLite syntax and error outright on any
    # other dialect — gate attaching the listener at all, rather than
    # gating inside it, so a Postgres connection never even offers the
    # chance to run one by mistake later.
    if engine.sync_engine.dialect.name == "sqlite":
        event.listen(
            engine.sync_engine, "connect", partial(_apply_sqlite_pragma, settings=settings)
        )
    return engine


def build_sync_engine(settings: Settings | None = None) -> Engine:
    settings = settings or get_settings()
    from sqlalchemy import create_engine

    # task-66 §1.2: pool bumped from the previous 5+10 default — live-
    # measured starved by ~140 threads across the api and worker processes
    # (see `Settings.sqlite_pool_size`'s own docstring). `pool_timeout`
    # dropped from 30s to `sqlite_pool_timeout_s` (5s) so a saturated pool
    # fails fast and visibly instead of hanging a request for half a minute.
    engine = create_engine(
        sqlite_sync_url(settings.database_url),
        pool_size=settings.sqlite_pool_size,
        max_overflow=settings.sqlite_pool_max_overflow,
        pool_timeout=settings.sqlite_pool_timeout_s,
    )
    # task-75: see the async engine's own comment above — same gate.
    if engine.dialect.name == "sqlite":
        event.listen(engine, "connect", partial(_apply_sqlite_pragma, settings=settings))
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


def session_has_pending_writes(session: Session) -> bool:
    """Does this session actually have anything to commit? (task-67 §3)

    Both signals are needed and neither alone is enough — live-probed
    against the real engine 2026-09-06:

    | what the session did      | ORM staged | DBAPI in_transaction |
    |---------------------------|------------|----------------------|
    | nothing                   | False      | False                |
    | SELECT (ORM or `text()`)  | False      | False                |
    | `execute(text("UPDATE"))` | False      | **True**             |
    | `session.add(...)`        | **True**   | False                |

    pysqlite only opens a transaction on actual DML, never on a SELECT, so
    `in_transaction` catches raw `execute()` writes that never touch the
    ORM identity map; `new/dirty/deleted` catches staged ORM work that has
    not been flushed to the driver yet.

    Biased towards `True`: `dirty` is "possibly modified" rather than
    "definitely", and an unrecognised driver falls through to `True`. A
    false positive costs one needless trip through the write gate; a false
    negative would let a real write skip the serialization entirely, which
    is the bug this whole gate exists to prevent.
    """
    if session.new or session.dirty or session.deleted:
        return True
    if not session.in_transaction():
        return False  # never used: no connection was even checked out
    raw = session.connection().connection.driver_connection
    return bool(getattr(raw, "in_transaction", True))


@contextmanager
def worker_session_scope() -> Generator[Session, None, None]:
    """Worker context manager: short transaction, commit on success, rollback on error.

    The commit itself (task-66 §2) is routed through `DbWriteGate` — the ONE
    global write queue every process-local caller of this function shares,
    plus an OS-level flock excluding the OTHER process (api vs worker) from
    committing at the same moment. This is the single choke point that lets
    every one of this function's ~25 existing call sites across the
    codebase (reports, monitor, imports, tech-audit, jobs, url-inspector, ...)
    get the serialization for free, with no per-call-site change needed.

    A READ-ONLY use skips the gate entirely (task-67 §3). The earlier
    assumption — that routing an empty commit through the gate too "costs
    a negligible queue round-trip, not a real write" — was wrong, and
    measurably so: the gate's job is wrapped in a CROSS-PROCESS flock, so
    a read-only endpoint queued behind whatever the other process happened
    to be writing. Live-measured 2026-09-06 during an ordinary archive
    capture, `GET /api/v1/monitor/changes` (which writes nothing at all):

        db_write_gate_slow_job duration_ms=2686 queue_wait_ms=0
        db_write_gate_slow_job duration_ms=4901 queue_wait_ms=0

    `queue_wait_ms=0` places every one of those seconds inside the flock
    wait, not behind other queued jobs. WAL gives unlimited concurrent
    readers for free; making them wait on the WRITE lock was our own doing.
    """
    from app.db.writegate import get_write_gate

    session = get_sync_sessionmaker()()
    try:
        yield session
        if session_has_pending_writes(session):
            get_write_gate().submit(session, lambda: None)
        else:
            session.commit()  # no writer lock is taken for an empty transaction
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
