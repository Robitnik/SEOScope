"""Daily off-site DB backup + GFS retention (task-52 §3.5).

Same online-backup technique as `scripts/backup_sqlite.py` (task-13):
`sqlite3.Connection.backup()`, never a raw file copy — the live DB is
written under WAL, and `cp app.db` without its `-wal`/`-shm` companions
would grab an inconsistent snapshot. Reimplemented here rather than
imported: `scripts/` has no `__init__.py` and is task-13's owned path, so
importing across that boundary would mean turning it into a package on
someone else's behalf for ~15 lines of stdlib sqlite calls.

GFS promotion (§3.5 step 5) is a server-side `CloudStore.copy()`, not a
re-upload: the daily snapshot is already in the bucket, so promoting it into
the weekly/monthly tier costs one API call and zero bytes of transfer.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
import os
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

from sqlalchemy.orm import Session

from app.cloud.client import CloudStore
from app.cloud.keys import db_backup_key, db_backup_manifest_key
from app.core.config import Settings
from app.db.repositories.cloud import CloudLedgerRepository
from app.db.session import sqlite_sync_url

logger = logging.getLogger(__name__)


def _sqlite_path(database_url: str) -> Path:
    sync_url = sqlite_sync_url(database_url)
    prefix = "sqlite:///"
    if not sync_url.startswith(prefix):
        raise ValueError(f"підтримується лише SQLite URL, отримано: {database_url!r}")
    return Path(sync_url[len(prefix) :])


@dataclass
class _Snapshot:
    path: Path
    alembic_revision: str | None


def _online_backup_and_check(source: Path) -> _Snapshot:
    fd, tmp_name = tempfile.mkstemp(suffix=".db", prefix="cloud-backup-")
    os.close(fd)
    target = Path(tmp_name)

    source_conn = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    target_conn = sqlite3.connect(target)
    try:
        source_conn.backup(target_conn)
    finally:
        target_conn.close()
        source_conn.close()

    verify = sqlite3.connect(f"file:{target}?mode=ro", uri=True)
    try:
        integrity = verify.execute("PRAGMA integrity_check").fetchone()[0]
        try:
            revision_row = verify.execute("SELECT version_num FROM alembic_version").fetchone()
        except sqlite3.OperationalError:
            revision_row = None
    finally:
        verify.close()

    if integrity != "ok":
        target.unlink(missing_ok=True)
        raise RuntimeError(f"бекап не пройшов integrity_check: {integrity}")

    return _Snapshot(path=target, alembic_revision=revision_row[0] if revision_row else None)


@dataclass
class BackupOutcome:
    timestamp: str
    date_key: str
    key: str
    manifest_key: str
    size_bytes: int
    sha256: str
    promoted_tiers: list[str]


def run_daily_backup(
    session: Session, store: CloudStore, settings: Settings
) -> BackupOutcome | None:
    """Returns `None` when disabled or when today's daily backup already
    exists (idempotent — safe to call more than once on the same UTC day,
    e.g. after a worker restart)."""
    if not store.enabled:
        return None

    ledger = CloudLedgerRepository(session)
    date_key = datetime.now(UTC).date().isoformat()
    if ledger.has_backup(tier="daily", date_key=date_key):
        return None

    source_path = _sqlite_path(settings.database_url)
    snapshot = _online_backup_and_check(source_path)
    try:
        raw_bytes = snapshot.path.read_bytes()
    finally:
        snapshot.path.unlink(missing_ok=True)

    compressed = gzip.compress(raw_bytes, compresslevel=6)
    sha256 = hashlib.sha256(compressed).hexdigest()

    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    key = db_backup_key("daily", timestamp)
    manifest_key = db_backup_manifest_key("daily", timestamp)
    manifest = {
        "sha256": sha256,
        "size_bytes": len(compressed),
        "raw_size_bytes": len(raw_bytes),
        "alembic_revision": snapshot.alembic_revision,
        "created_at": datetime.now(UTC).isoformat(),
    }

    store.put_bytes(compressed, key, content_type="application/gzip")
    store.put_bytes(
        json.dumps(manifest, ensure_ascii=False).encode("utf-8"),
        manifest_key,
        content_type="application/json",
    )
    ledger.record_backup(
        tier="daily", date_key=date_key, key=key, sha256=sha256, size_bytes=len(compressed)
    )
    ledger.commit()
    logger.info(
        "cloud_backup_uploaded tier=daily size_bytes=%d sha256=%s", len(compressed), sha256[:12]
    )

    promoted = _promote_if_due(
        session,
        store,
        timestamp=timestamp,
        date_key=date_key,
        sha256=sha256,
        size_bytes=len(compressed),
    )

    return BackupOutcome(
        timestamp=timestamp,
        date_key=date_key,
        key=key,
        manifest_key=manifest_key,
        size_bytes=len(compressed),
        sha256=sha256,
        promoted_tiers=promoted,
    )


def _promote_if_due(
    session: Session,
    store: CloudStore,
    *,
    timestamp: str,
    date_key: str,
    sha256: str,
    size_bytes: int,
) -> list[str]:
    ledger = CloudLedgerRepository(session)
    promoted: list[str] = []
    today = date.fromisoformat(date_key)
    daily_key = db_backup_key("daily", timestamp)
    daily_manifest_key = db_backup_manifest_key("daily", timestamp)

    def _promote(tier: str) -> None:
        tier_key = db_backup_key(tier, timestamp)
        tier_manifest_key = db_backup_manifest_key(tier, timestamp)
        store.copy(daily_key, tier_key)
        store.copy(daily_manifest_key, tier_manifest_key)
        ledger.record_backup(
            tier=tier, date_key=date_key, key=tier_key, sha256=sha256, size_bytes=size_bytes
        )
        ledger.commit()
        promoted.append(tier)
        logger.info("cloud_backup_promoted tier=%s date=%s", tier, date_key)

    # Sunday closes the ISO week — matches the "12 weekly" retention being a
    # quarter of history at one-a-week granularity (task-52 §3.1/§7).
    if today.weekday() == 6 and not ledger.has_backup(tier="weekly", date_key=date_key):
        _promote("weekly")
    if today.day == 1 and not ledger.has_backup(tier="monthly", date_key=date_key):
        _promote("monthly")
    return promoted


def prune_backups(
    session: Session, store: CloudStore, settings: Settings, *, dry_run: bool = False
) -> dict[str, int]:
    """GFS retention (task-52 §3.5 step 6): keep the N newest uploaded
    backups per tier, delete the rest (object + manifest + ledger row).
    Never deletes when a tier has AT MOST its keep-count — a sorting bug
    here must never be able to erase the last remaining copy."""
    ledger = CloudLedgerRepository(session)
    keep_by_tier = {
        "daily": settings.cloud_backup_keep_daily,
        "weekly": settings.cloud_backup_keep_weekly,
        "monthly": settings.cloud_backup_keep_monthly,
    }
    removed_counts: dict[str, int] = {}
    for tier, keep in keep_by_tier.items():
        backups = ledger.backups_by_tier(tier)  # newest first
        stale = backups[keep:] if keep > 0 else []
        removed_counts[tier] = len(stale)
        if dry_run or not stale:
            continue
        for obj in stale:
            manifest_key = obj.key.replace(".db.gz", ".manifest.json")
            store.delete(obj.key)
            store.delete(manifest_key)
            ledger.delete_backup_row(obj)
        ledger.commit()
        logger.info("cloud_backup_pruned tier=%s removed=%d", tier, len(stale))
    return removed_counts
