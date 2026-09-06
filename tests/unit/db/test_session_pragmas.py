"""task-66 §1 — every PRAGMA/pool setting is live-measured, not assumed
(see the task plan's §0 for the actual numbers). These tests verify the
values a fresh connection/engine actually gets, not just that the code
compiles.
"""

from __future__ import annotations

from sqlalchemy import text

from app.core.config import Settings
from app.db.session import build_sync_engine, default_write_lock_path


def _settings(tmp_path, **overrides) -> Settings:
    return Settings(
        _env_file=None,
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        **overrides,
    )


def test_new_connection_gets_every_pragma_from_settings(tmp_path):
    settings = _settings(
        tmp_path,
        sqlite_synchronous="NORMAL",
        sqlite_busy_timeout_ms=5000,
        sqlite_cache_kib=65536,
        sqlite_mmap_bytes=268435456,
    )
    engine = build_sync_engine(settings)
    try:
        with engine.connect() as conn:
            assert conn.execute(text("PRAGMA journal_mode")).scalar() == "wal"
            assert conn.execute(text("PRAGMA foreign_keys")).scalar() == 1
            assert conn.execute(text("PRAGMA synchronous")).scalar() == 1  # NORMAL's own code
            assert conn.execute(text("PRAGMA busy_timeout")).scalar() == 5000
            assert conn.execute(text("PRAGMA cache_size")).scalar() == -65536
            assert conn.execute(text("PRAGMA mmap_size")).scalar() == 268435456
            assert conn.execute(text("PRAGMA temp_store")).scalar() == 2  # MEMORY's own code
    finally:
        engine.dispose()


def test_pool_is_sized_from_settings_not_hardcoded(tmp_path):
    settings = _settings(tmp_path, sqlite_pool_size=7, sqlite_pool_max_overflow=3)
    engine = build_sync_engine(settings)
    try:
        assert engine.pool.size() == 7
        assert engine.pool._max_overflow == 3
    finally:
        engine.dispose()


def test_pool_timeout_is_short_by_default(tmp_path):
    settings = _settings(tmp_path)
    engine = build_sync_engine(settings)
    try:
        assert engine.pool._timeout == settings.sqlite_pool_timeout_s
        assert settings.sqlite_pool_timeout_s <= 5.0
    finally:
        engine.dispose()


def test_default_write_lock_path_is_derived_from_database_url(tmp_path):
    settings = _settings(tmp_path)
    lock_path = default_write_lock_path(settings)
    assert lock_path == (tmp_path / "test.db.write.lock")


def test_explicit_write_lock_path_overrides_the_derived_default(tmp_path):
    explicit = tmp_path / "elsewhere.lock"
    settings = _settings(tmp_path, db_write_lock_path=explicit)
    assert default_write_lock_path(settings) == explicit


def test_two_settings_with_different_database_urls_never_share_a_lock_path(tmp_path):
    """The exact hazard this function exists to prevent (task-66 §2.1): a
    fixed lock path would make an isolated test database share ONE lock
    with the real production database bind-mounted into the same
    container.
    """
    a = _settings(tmp_path)
    b = Settings(_env_file=None, database_url="sqlite:///./app_data/app.db")
    assert default_write_lock_path(a) != default_write_lock_path(b)


def test_wal_autocheckpoint_is_disabled_by_default(tmp_path):
    """task-67 §2: the single biggest measured win in this area. SQLite's
    stock 1000-page autocheckpoint makes whichever ordinary commit happens
    to overflow the WAL pay for reclaiming it, holding the writer lock for
    a measured 334-432ms. At `0` that work moves to `DbWriteGate`'s idle
    checkpointer: measured ~4-5x on throughput and up to ~50x on the
    longest stall any other writer can see.
    """
    engine = build_sync_engine(_settings(tmp_path))
    try:
        with engine.connect() as conn:
            assert conn.execute(text("PRAGMA wal_autocheckpoint")).scalar() == 0
    finally:
        engine.dispose()


def test_wal_autocheckpoint_can_be_restored_from_settings(tmp_path):
    """The escape hatch: `sqlite_wal_autocheckpoint` exists so SQLite's own
    behaviour can be restored without a code change if the idle
    checkpointer is ever suspected of misbehaving in production.
    """
    engine = build_sync_engine(_settings(tmp_path, sqlite_wal_autocheckpoint=1000))
    try:
        with engine.connect() as conn:
            assert conn.execute(text("PRAGMA wal_autocheckpoint")).scalar() == 1000
    finally:
        engine.dispose()


def test_sqlite_db_path_is_derived_from_database_url(tmp_path):
    """`DbWriteGate`'s checkpointer has to find the `-wal` sibling on disk,
    so it needs the real file behind the (async-flavoured) URL.
    """
    from app.db.session import sqlite_db_path

    settings = Settings(_env_file=None, database_url=f"sqlite+aiosqlite:///{tmp_path / 'x.db'}")
    assert sqlite_db_path(settings) == tmp_path / "x.db"
    assert default_write_lock_path(settings) == tmp_path / "x.db.write.lock"


def _pending(session_factory, body):
    from app.db.session import session_has_pending_writes

    session = session_factory()
    try:
        body(session)
        return session_has_pending_writes(session)
    finally:
        session.rollback()
        session.close()


def test_read_only_sessions_are_not_reported_as_pending_writes(tmp_path):
    """task-67 §3. A read-only session that goes through the write gate
    waits on the CROSS-PROCESS flock — live-measured at 2686ms and 4901ms
    with `queue_wait_ms=0` (i.e. entirely flock wait) on
    `GET /api/v1/monitor/changes`, an endpoint that writes nothing. WAL
    gives unlimited concurrent readers; making them queue behind the write
    lock was our own doing.
    """
    from sqlalchemy.orm import sessionmaker

    engine = build_sync_engine(_settings(tmp_path))
    try:
        with engine.begin() as conn:
            conn.execute(text("create table t(id integer primary key, n integer)"))
            conn.execute(text("insert into t(id, n) values (1, 1)"))
        factory = sessionmaker(bind=engine)

        assert _pending(factory, lambda s: None) is False
        read = lambda s: s.execute(text("select count(*) from t")).scalar()  # noqa: E731
        assert _pending(factory, read) is False
        # A raw DML `execute()` never touches the ORM identity map, so
        # `new/dirty/deleted` stays empty — only pysqlite's own
        # `in_transaction` catches it. Missing this would let a real write
        # skip the gate, which is far worse than a needless trip through it.
        assert _pending(factory, lambda s: s.execute(text("update t set n=2 where id=1"))) is True
    finally:
        engine.dispose()


def test_async_pool_is_sized_too_not_left_on_sqlalchemy_defaults(tmp_path):
    """task-67. task-66 §1.2 sized the SYNC engine and left the async one on
    SQLAlchemy's stock 5 + 10 / 30s — the half that actually serves every
    `async def` endpoint, `/dashboard` included.
    """
    from app.db.session import build_async_engine

    settings = Settings(
        _env_file=None,
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'test.db'}",
        sqlite_async_pool_size=16,
        sqlite_async_pool_max_overflow=16,
        sqlite_pool_timeout_s=5.0,
    )
    engine = build_async_engine(settings)
    try:
        pool = engine.pool
        assert pool.size() == 16
        assert pool._max_overflow == 16
        assert pool._timeout == 5.0
    finally:
        engine.sync_engine.dispose()
