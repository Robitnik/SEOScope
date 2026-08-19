"""`CloudScheduler` (task-52 §3.8): disabled means literally no thread, the
daily backup runs exactly once no matter how many ticks land after the
configured time, and one tick sweeps both kinds — all against a real temp
SQLite file (the same one `engine` already points at) and `FakeCloudStore`.
"""

from __future__ import annotations

import datetime as datetime_module
from contextlib import contextmanager
from pathlib import Path

import pytest
from sqlalchemy.orm import sessionmaker

from app.cloud.scheduler import CloudScheduler
from app.core.config import Settings
from app.db.models import MonitorBlob
from app.db.repositories.clients import ClientRepository
from app.db.repositories.cloud import CloudLedgerRepository

from .fakes import FakeCloudStore


def _scope(engine):
    maker = sessionmaker(bind=engine, expire_on_commit=False)

    @contextmanager
    def scope():
        session = maker()
        try:
            yield session
            session.commit()
        finally:
            session.close()

    return scope


def test_disabled_scheduler_never_starts_a_thread(engine):
    settings = Settings(_env_file=None, cloud_enabled=False)
    scheduler = CloudScheduler(
        session_scope=_scope(engine), settings=settings, store=FakeCloudStore(enabled=False)
    )
    scheduler.start()
    assert scheduler._thread is None
    scheduler.stop()  # must be a safe no-op, not an AttributeError on a never-started thread


def test_run_backup_if_due_uploads_exactly_once_across_repeated_calls(engine):
    db_path = engine.url.database
    settings = Settings(
        _env_file=None,
        cloud_enabled=True,
        b2_vault_bucket_name="vault",
        b2_vault_key_id="key-id",
        b2_vault_application_key="secret",
        database_url=f"sqlite:///{db_path}",
        cloud_backup_at_utc="00:00",  # always "due" — 00:00 can never be in the future
    )
    store = FakeCloudStore()
    scheduler = CloudScheduler(session_scope=_scope(engine), settings=settings, store=store)

    for _ in range(5):
        scheduler._run_backup_if_due()

    assert len(store.uploads) == 2  # the .db.gz and its manifest, only once
    with sessionmaker(bind=engine, expire_on_commit=False)() as session:
        assert CloudLedgerRepository(session).latest_backup() is not None


def test_run_backup_if_due_skips_before_the_configured_time(
    engine, monkeypatch: pytest.MonkeyPatch
):
    class _FixedDatetime(datetime_module.datetime):
        @classmethod
        def now(cls, tz=None):  # noqa: ANN001 - matches datetime.now's own signature
            return cls(2026, 8, 19, 2, 0, tzinfo=tz)

    monkeypatch.setattr("app.cloud.scheduler.datetime", _FixedDatetime)
    db_path = engine.url.database
    settings = Settings(
        _env_file=None,
        cloud_enabled=True,
        b2_vault_bucket_name="vault",
        b2_vault_key_id="key-id",
        b2_vault_application_key="secret",
        database_url=f"sqlite:///{db_path}",
        cloud_backup_at_utc="03:30",
    )
    store = FakeCloudStore()
    scheduler = CloudScheduler(session_scope=_scope(engine), settings=settings, store=store)

    scheduler._run_backup_if_due()

    assert store.uploads == []


def test_tick_sweeps_monitor_blobs_and_report_files(engine, tmp_path: Path):
    db_path = engine.url.database
    settings = Settings(
        _env_file=None,
        cloud_enabled=True,
        b2_vault_bucket_name="vault",
        b2_vault_key_id="key-id",
        b2_vault_application_key="secret",
        database_url=f"sqlite:///{db_path}",
        artifacts_dir=tmp_path,
        cloud_backup_at_utc="23:59",  # keep this tick's backup check a no-op
    )
    store = FakeCloudStore()
    scheduler = CloudScheduler(session_scope=_scope(engine), settings=settings, store=store)

    blob_path = tmp_path / "monitor" / "aa" / ("a" * 64 + ".gz")
    blob_path.parent.mkdir(parents=True, exist_ok=True)
    blob_path.write_bytes(b"gzip-bytes")
    with sessionmaker(bind=engine, expire_on_commit=False)() as session:
        # Fix the watermark to well before this blob's timestamp — otherwise
        # the FIRST sweep sets it to "now" and could exclude a blob inserted
        # a moment earlier in this same test (task-52 §3.4 "only new").
        from datetime import UTC, datetime

        from app.cloud.sweeper import ARCHIVE_WATERMARK

        CloudLedgerRepository(session).set_watermark(
            ARCHIVE_WATERMARK, datetime(2000, 1, 1, tzinfo=UTC)
        )
        session.add(
            MonitorBlob(
                sha256="a" * 64,
                size_bytes=20,
                gzip_size_bytes=10,
                mime="text/html",
                path=f"monitor/aa/{'a' * 64}.gz",
            )
        )
        ClientRepository(session).create(
            name="Acme", slug="acme", domain="acme.com", timezone="America/New_York"
        )
        session.commit()

    scheduler._tick()

    assert any(key.startswith("archive/") for key in store.objects)
