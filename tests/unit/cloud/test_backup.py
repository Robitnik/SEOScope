"""Daily DB backup + GFS retention (task-52 §3.5) — a real temp SQLite file
through the actual online-backup path, uploaded to `FakeCloudStore`.
"""

from __future__ import annotations

import datetime as datetime_module
import json
import sqlite3
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from app.cloud.backup import prune_backups, run_daily_backup
from app.core.config import Settings
from app.db.repositories.cloud import CloudLedgerRepository

from .fakes import FakeCloudStore


def _make_sqlite_db(path: Path, *, revision: str = "deadbeef0000") -> None:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE alembic_version (version_num VARCHAR(32))")
    conn.execute("INSERT INTO alembic_version VALUES (?)", (revision,))
    conn.execute("CREATE TABLE clients (id TEXT)")
    conn.commit()
    conn.close()


def _settings(db_path: Path, **overrides: object) -> Settings:
    return Settings(
        _env_file=None,
        cloud_enabled=True,
        database_url=f"sqlite:///{db_path}",
        b2_vault_bucket_name="vault",
        b2_vault_key_id="key-id",
        b2_vault_application_key="secret",
        **overrides,
    )


def test_run_daily_backup_uploads_db_and_manifest(session: Session, tmp_path: Path):
    db_path = tmp_path / "app.db"
    _make_sqlite_db(db_path)
    store = FakeCloudStore()

    outcome = run_daily_backup(session, store, _settings(db_path))

    assert outcome is not None
    assert outcome.key in store.objects
    assert outcome.manifest_key in store.objects
    manifest = json.loads(store.objects[outcome.manifest_key])
    assert manifest["sha256"] == outcome.sha256
    assert manifest["alembic_revision"] == "deadbeef0000"

    ledger = CloudLedgerRepository(session)
    assert ledger.has_backup(tier="daily", date_key=outcome.date_key)


def test_run_daily_backup_is_idempotent_same_day(session: Session, tmp_path: Path):
    db_path = tmp_path / "app.db"
    _make_sqlite_db(db_path)
    store = FakeCloudStore()
    settings = _settings(db_path)

    first = run_daily_backup(session, store, settings)
    assert first is not None
    uploaded_before = len(store.uploads)

    second = run_daily_backup(session, store, settings)

    assert second is None
    assert len(store.uploads) == uploaded_before  # no new network calls


def test_run_daily_backup_uploads_nothing_on_a_corrupt_snapshot(
    session: Session, tmp_path: Path
):
    db_path = tmp_path / "app.db"
    db_path.write_bytes(b"this is not a sqlite database at all")
    store = FakeCloudStore()

    with pytest.raises(Exception):  # noqa: B017, PT011 - sqlite3 raises its own error type here
        run_daily_backup(session, store, _settings(db_path))

    assert store.uploads == []
    assert CloudLedgerRepository(session).latest_backup() is None


def test_run_daily_backup_disabled_store_returns_none(session: Session, tmp_path: Path):
    db_path = tmp_path / "app.db"
    _make_sqlite_db(db_path)
    store = FakeCloudStore(enabled=False)

    outcome = run_daily_backup(session, store, _settings(db_path))

    assert outcome is None
    assert store.uploads == []


def test_promotion_to_weekly_on_sunday(
    session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    class _FixedDatetime(datetime_module.datetime):
        @classmethod
        def now(cls, tz=None):  # noqa: ANN001 - matches datetime.now's own signature
            return cls(2026, 8, 16, 3, 30, tzinfo=tz)  # a Sunday

    monkeypatch.setattr("app.cloud.backup.datetime", _FixedDatetime)
    db_path = tmp_path / "app.db"
    _make_sqlite_db(db_path)
    store = FakeCloudStore()

    outcome = run_daily_backup(session, store, _settings(db_path))

    assert outcome is not None
    assert outcome.promoted_tiers == ["weekly"]
    assert CloudLedgerRepository(session).has_backup(tier="weekly", date_key=outcome.date_key)


def test_promotion_to_monthly_on_the_first(
    session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    class _FixedDatetime(datetime_module.datetime):
        @classmethod
        def now(cls, tz=None):  # noqa: ANN001 - matches datetime.now's own signature
            return cls(2026, 9, 1, 3, 30, tzinfo=tz)

    monkeypatch.setattr("app.cloud.backup.datetime", _FixedDatetime)
    db_path = tmp_path / "app.db"
    _make_sqlite_db(db_path)
    store = FakeCloudStore()

    outcome = run_daily_backup(session, store, _settings(db_path))

    assert outcome is not None
    assert "monthly" in outcome.promoted_tiers
    assert CloudLedgerRepository(session).has_backup(tier="monthly", date_key=outcome.date_key)


def test_no_promotion_on_an_ordinary_weekday(
    session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    class _FixedDatetime(datetime_module.datetime):
        @classmethod
        def now(cls, tz=None):  # noqa: ANN001 - matches datetime.now's own signature
            return cls(2026, 8, 19, 3, 30, tzinfo=tz)  # a Wednesday, not the 1st

    monkeypatch.setattr("app.cloud.backup.datetime", _FixedDatetime)
    db_path = tmp_path / "app.db"
    _make_sqlite_db(db_path)
    store = FakeCloudStore()

    outcome = run_daily_backup(session, store, _settings(db_path))

    assert outcome is not None
    assert outcome.promoted_tiers == []


# --- prune_backups (GFS retention) ------------------------------------------


def _seed_daily_backups(session: Session, store: FakeCloudStore, count: int) -> None:
    ledger = CloudLedgerRepository(session)
    for i in range(count):
        date_key = f"2026-08-{i + 1:02d}"
        key = f"db/daily/app-{date_key}.db.gz"
        manifest_key = f"db/daily/app-{date_key}.manifest.json"
        store.objects[key] = b"snapshot"
        store.objects[manifest_key] = b"{}"
        ledger.record_backup(tier="daily", date_key=date_key, key=key, sha256="x", size_bytes=8)
    session.commit()


def test_prune_backups_keeps_exactly_the_configured_count(session: Session, tmp_path: Path):
    db_path = tmp_path / "app.db"
    _make_sqlite_db(db_path)
    store = FakeCloudStore()
    settings = _settings(db_path, cloud_backup_keep_daily=3)
    _seed_daily_backups(session, store, 5)

    removed = prune_backups(session, store, settings)

    assert removed["daily"] == 2
    assert len(CloudLedgerRepository(session).backups_by_tier("daily")) == 3


def test_prune_backups_never_deletes_when_at_or_under_the_limit(
    session: Session, tmp_path: Path
):
    db_path = tmp_path / "app.db"
    _make_sqlite_db(db_path)
    store = FakeCloudStore()
    settings = _settings(db_path, cloud_backup_keep_daily=30)
    _seed_daily_backups(session, store, 3)

    removed = prune_backups(session, store, settings)

    assert removed["daily"] == 0
    assert len(CloudLedgerRepository(session).backups_by_tier("daily")) == 3
    assert len(store.objects) == 6  # 3 keys + 3 manifests, nothing deleted


def test_prune_backups_dry_run_deletes_nothing(session: Session, tmp_path: Path):
    db_path = tmp_path / "app.db"
    _make_sqlite_db(db_path)
    store = FakeCloudStore()
    settings = _settings(db_path, cloud_backup_keep_daily=1)
    _seed_daily_backups(session, store, 3)

    removed = prune_backups(session, store, settings, dry_run=True)

    assert removed["daily"] == 2
    assert len(CloudLedgerRepository(session).backups_by_tier("daily")) == 3
    assert len(store.objects) == 6


def test_prune_backups_removes_the_object_and_its_manifest(session: Session, tmp_path: Path):
    db_path = tmp_path / "app.db"
    _make_sqlite_db(db_path)
    store = FakeCloudStore()
    settings = _settings(db_path, cloud_backup_keep_daily=1)
    _seed_daily_backups(session, store, 2)

    prune_backups(session, store, settings)

    assert "db/daily/app-2026-08-01.db.gz" not in store.objects
    assert "db/daily/app-2026-08-01.manifest.json" not in store.objects
    assert "db/daily/app-2026-08-02.db.gz" in store.objects
