"""Pull-sweeper (task-52 §3.4): the ONLY way bytes reach the cloud for
monitor blobs and report files.

Reads `app/monitor/**`'s and `app/reports/**`'s own existing tables
(`monitor_blobs`, `report_files`) and uploads whatever `CloudLedgerRepository`
doesn't yet have an `uploaded` row for. Deliberately NOT a hook inside
`store_blob()`/`ReportFile` creation: those modules are task-50/51's active
owned paths, and putting a network call on that hot path would make B2
latency part of collection reliability — a timeout there would fail a
client's monitor run or report, not just a backup.

Each candidate is committed individually (`ledger.commit()` per item, not
once per batch): a crash mid-sweep loses nothing already written, and the
next tick simply re-derives its candidate list from what's still missing —
no separate recovery step needed, unlike `MonitorScheduler`/`ReportScheduler`
which must reconcile stale `running` rows on restart.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.cloud.client import CloudStore
from app.cloud.keys import monitor_blob_key, report_object_key
from app.core.config import Settings
from app.db.repositories.clients import ClientRepository
from app.db.repositories.cloud import CloudLedgerRepository
from app.db.repositories.monitor import MonitorRepository
from app.monitor.storage import delete_blob_file, resolve_blob_path

logger = logging.getLogger(__name__)

# Fixed watermark name (task-52 §3.4): the moment cloud sync activated for
# this install. Set once, on first tick, and never moved — a later
# `cloud-sync --since` backfill is a deliberate one-off CLI action, not a
# change to what the regular sweeper considers "new".
ARCHIVE_WATERMARK = "archive_since"


@dataclass
class SweepResult:
    attempted: int = 0
    uploaded: int = 0
    failed: int = 0
    reasons: dict[str, int] = field(default_factory=dict)

    def note_failure(self, reason: str) -> None:
        self.failed += 1
        self.reasons[reason] = self.reasons.get(reason, 0) + 1


@dataclass
class PruneResult:
    candidates: int = 0
    deferred_not_uploaded: int = 0
    removed: int = 0
    chunks_failed: int = 0


# Rows are deleted and committed in chunks this size, then their files go.
# Small enough that the write lock is held briefly (the monitor's own four
# threads are writing to the same SQLite file from the same process, and
# the CLI is a whole separate process where `_COMMIT_LOCK` means nothing),
# and small enough that a lock failure costs one chunk, not the whole pass.
PRUNE_CHUNK_SIZE = 250


def prune_monitor_bodies(
    session: Session,
    settings: Settings,
    *,
    older_than_days: int,
    store: CloudStore | None = None,
    dry_run: bool = False,
    include_unreferenced: bool = False,
) -> PruneResult:
    """Drop locally-stored page/resource bodies whose every referencing
    version is older than `older_than_days` — history (signals, hashes,
    change feed) is untouched; only the raw bytes go (task-35 §3.2.1's
    `monitor-prune --bodies-older-than`, made cloud-aware for task-52's
    daily schedule).

    Cloud-gated by construction: when cloud sync is on for monitor blobs,
    a body is only ever removed once `CloudLedgerRepository` confirms THAT
    exact sha256 is `uploaded` — age alone is never enough. This is the
    fix for a real gap found live 2026-09-04: the archive watermark only
    covers blobs created after cloud sync first activated, so anything
    older sat in `monitor_blobs` with no cloud copy at all; pruning by age
    alone would have deleted it a second time, for good. When cloud sync
    is off (or this store has no credentials), there is no safety net to
    defer to and pruning falls back to age-only — same as before this
    existed.

    DB first, file second, in committed chunks — the ordering matters and
    was learned the hard way (live 2026-09-04): the original loop deleted
    each FILE inside the loop and committed the rows only at the very end,
    so a mid-pass `database is locked` rolled the rows back while 7,341
    files were already gone from disk, leaving every one of those rows
    pointing at nothing. Committing the rows first means the worst a lock
    failure can do is leave a body on disk that no row claims — wasted
    space, reclaimed on the next pass — instead of a row that claims a body
    which no longer exists.
    """
    result = PruneResult()
    if older_than_days <= 0 and not include_unreferenced:
        return result

    monitor_repo = MonitorRepository(session)
    candidate_shas: set[str] = set()
    if older_than_days > 0:
        cutoff = datetime.now(UTC) - timedelta(days=older_than_days)
        candidate_shas |= set(monitor_repo.blobs_referenced_only_by_versions_older_than(cutoff))
    if include_unreferenced:
        candidate_shas |= {blob.sha256 for blob in monitor_repo.unreferenced_blobs()}
    result.candidates = len(candidate_shas)
    if not candidate_shas:
        return result

    cloud_gated = (store is not None and store.enabled) and settings.cloud_sync_monitor_blobs
    if cloud_gated:
        ledger = CloudLedgerRepository(session)
        removable = ledger.uploaded_source_refs("monitor_blob", candidate_shas)
        result.deferred_not_uploaded = len(candidate_shas) - len(removable)
    else:
        removable = candidate_shas

    if dry_run:
        result.removed = len(removable)
        return result

    ordered = sorted(removable)
    for start in range(0, len(ordered), PRUNE_CHUNK_SIZE):
        chunk = ordered[start : start + PRUNE_CHUNK_SIZE]
        try:
            for sha256 in chunk:
                monitor_repo.delete_blob(sha256)
            session.commit()
        except OperationalError:
            # Almost always "database is locked": the monitor is mid-write.
            # Nothing on disk has been touched for this chunk yet, so the
            # rows and their files are still consistent — drop this chunk
            # and let the next daily pass pick it up again.
            session.rollback()
            result.chunks_failed += 1
            logger.warning(
                "monitor_prune_chunk_locked skipped=%d start=%d", len(chunk), start
            )
            continue
        for sha256 in chunk:
            delete_blob_file(settings.artifacts_dir, sha256)
        result.removed += len(chunk)

    return result


def sweep_monitor_blobs(
    session: Session, store: CloudStore, settings: Settings, *, limit: int | None = None
) -> SweepResult:
    result = SweepResult()
    if not (store.enabled and settings.cloud_sync_monitor_blobs):
        return result

    ledger = CloudLedgerRepository(session)
    since = ledger.get_or_init_watermark(ARCHIVE_WATERMARK, default=datetime.now(UTC))
    ledger.commit()
    batch_limit = limit if limit is not None else settings.cloud_sweep_batch_size
    candidates = ledger.monitor_blob_candidates(since=since, limit=batch_limit)

    for blob in candidates:
        result.attempted += 1
        key = monitor_blob_key(blob.sha256)
        local_path = resolve_blob_path(settings.artifacts_dir, blob.sha256)
        if not local_path.is_file():
            # Honest state (PLAN §12), not a silent skip: the blob row and
            # the file it points at can drift apart (manual cleanup, disk
            # issue) — recording WHY beats leaving it invisible forever.
            ledger.record_failed(
                kind="monitor_blob",
                source_ref=blob.sha256,
                key=key,
                error_message="local_file_missing",
            )
            ledger.commit()
            result.note_failure("local_file_missing")
            continue
        try:
            # Already gzip on disk (app.monitor.storage.store_blob writes
            # compressed bytes) — uploaded as-is, no re-compression.
            data = local_path.read_bytes()
            store.put_bytes(data, key, content_type="application/gzip")
        except Exception as exc:  # noqa: BLE001 - one blob's failure must never abort the batch
            logger.warning(
                "cloud_sweep_monitor_blob_failed sha256=%s error=%s",
                blob.sha256,
                type(exc).__name__,
            )
            ledger.record_failed(
                kind="monitor_blob", source_ref=blob.sha256, key=key, error_message=str(exc)[:1000]
            )
            ledger.commit()
            result.note_failure("upload_error")
            continue

        ledger.record_uploaded(
            kind="monitor_blob",
            source_ref=blob.sha256,
            key=key,
            # sha256 of the UPLOADED (gzip) bytes, not `blob.sha256` — that
            # column is the content-address of the raw, uncompressed body
            # (`app.monitor.storage.store_blob`), which will never equal a
            # hash of what actually sits at `key`. This is what `cloud-verify`
            # compares a re-downloaded object against.
            sha256=hashlib.sha256(data).hexdigest(),
            size_bytes=blob.gzip_size_bytes,
        )
        ledger.commit()
        result.uploaded += 1

    return result


def sweep_report_files(
    session: Session, store: CloudStore, settings: Settings, *, limit: int | None = None
) -> SweepResult:
    result = SweepResult()
    if not (store.enabled and settings.cloud_sync_reports):
        return result

    ledger = CloudLedgerRepository(session)
    clients_repo = ClientRepository(session)
    batch_limit = limit if limit is not None else settings.cloud_sweep_batch_size
    candidates = ledger.report_file_candidates(limit=batch_limit)

    for report_file in candidates:
        result.attempted += 1
        client = clients_repo.get(report_file.client_id)
        client_slug = client.slug if client is not None else "unknown-client"
        year = report_file.created_at.year
        file_name = report_file.path.rsplit("/", 1)[-1]
        key = report_object_key(client_slug, year, file_name)
        if ledger.report_file_key_taken_by_other(key, exclude_source_ref=report_file.id):
            key = report_object_key(
                client_slug, year, file_name, disambiguator=report_file.sha256[:8]
            )

        local_path = settings.artifacts_dir / report_file.path
        if not local_path.is_file():
            ledger.record_failed(
                kind="report_file",
                source_ref=report_file.id,
                key=key,
                error_message="local_file_missing",
            )
            ledger.commit()
            result.note_failure("local_file_missing")
            continue
        try:
            store.put_file(local_path, key, content_type=report_file.mime)
        except Exception as exc:  # noqa: BLE001 - one file's failure must never abort the batch
            logger.warning(
                "cloud_sweep_report_file_failed report_file_id=%s error=%s",
                report_file.id,
                type(exc).__name__,
            )
            ledger.record_failed(
                kind="report_file",
                source_ref=report_file.id,
                key=key,
                error_message=str(exc)[:1000],
            )
            ledger.commit()
            result.note_failure("upload_error")
            continue

        ledger.record_uploaded(
            kind="report_file",
            source_ref=report_file.id,
            key=key,
            sha256=report_file.sha256,
            size_bytes=report_file.size_bytes,
        )
        ledger.commit()
        result.uploaded += 1

    return result
