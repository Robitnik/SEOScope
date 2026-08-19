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
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.cloud.client import CloudStore
from app.cloud.keys import monitor_blob_key, report_object_key
from app.core.config import Settings
from app.db.repositories.clients import ClientRepository
from app.db.repositories.cloud import CloudLedgerRepository
from app.monitor.storage import resolve_blob_path

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
