"""Pull-sweeper for the Web Archive's own B2 upload (task-59 §1.8).

Mirrors `app.cloud.sweeper`'s pattern for `monitor_blobs` — the ONLY way
archive bytes reach the cloud — but is entirely self-contained: its own key
layout (`webarchive/` prefix, distinct from task-52's `archive/`) and its
own ledger (`ArchiveBlob.uploaded_at`, not `CloudLedgerRepository`/
`cloud_objects`), so zero edits to `app/cloud/**` are needed. Uses
`app.cloud.client.CloudStore` purely as a library — the ONLY module in this
codebase allowed to import `b2sdk` directly (task-52 §3.2) — never talks to
B2 itself.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.cloud.client import CloudStore
from app.core.config import Settings
from app.db.models import ArchiveBlob
from app.db.repositories.web_archive import WebArchiveRepository
from app.webarchive import storage
from app.webarchive.db_retry import commit_with_retry

logger = logging.getLogger(__name__)


def archive_blob_key(prefix: str, sha256: str) -> str:
    """`webarchive/<sha[:2]>/<sha>.gz` — mirrors
    `app.monitor.storage.blob_relative_path`'s local layout one-to-one,
    under its own prefix; the sha256 IS the address, so a re-upload of the
    same bytes is inherently idempotent (task-52 §3.1's own convention for
    `monitor_blob_key`, followed here without touching that file).
    """
    return f"{prefix}/{sha256[:2]}/{sha256}.gz"


@dataclass
class SweepResult:
    attempted: int = 0
    uploaded: int = 0
    failed: int = 0
    reasons: dict[str, int] = field(default_factory=dict)

    def note_failure(self, reason: str) -> None:
        self.failed += 1
        self.reasons[reason] = self.reasons.get(reason, 0) + 1


def sweep_archive_blobs(
    session: Session, store: CloudStore, settings: Settings, *, batch_size: int | None = None
) -> SweepResult:
    """Upload every `archive_blobs` row with `uploaded_at IS NULL`, one at a
    time, committing after each — a crash mid-sweep loses nothing already
    uploaded, and the next tick simply re-derives its candidate list from
    what is still missing (same reasoning `app.cloud.sweeper` documents for
    monitor blobs).
    """
    result = SweepResult()
    if not store.enabled:
        return result
    repo = WebArchiveRepository(session)
    candidates = repo.blobs_not_uploaded(limit=batch_size or settings.cloud_sweep_batch_size)
    artifacts_dir = settings.artifacts_dir
    for blob in candidates:
        result.attempted += 1
        local_path = storage.resolve_blob_path(artifacts_dir, blob.sha256)
        if not local_path.exists():
            result.note_failure("missing_local_file")
            continue
        try:
            # `commit_with_retry` re-runs the upload itself on a transient
            # `database is locked` (task-59 live incident, 2026-09-04) — safe
            # here too: B2 upload is idempotent on this content-addressed
            # key, so a retried re-upload of the same bytes is a no-op cost,
            # never a duplicate or a corruption.
            upload = partial(_upload_one, repo, store, settings, local_path, blob)
            commit_with_retry(session, upload)
            result.uploaded += 1
        except Exception as exc:  # noqa: BLE001 - one bad blob must never stop the sweep
            session.rollback()
            logger.warning(
                "archive_blob_upload_failed sha256=%s error=%s", blob.sha256, type(exc).__name__
            )
            result.note_failure(type(exc).__name__)
    return result


def _upload_one(
    repo: WebArchiveRepository, store: CloudStore, settings: Settings,
    local_path: Path, blob: ArchiveBlob,
) -> None:
    key = archive_blob_key(settings.archive_cloud_prefix, blob.sha256)
    store.put_file(local_path, key, content_type=blob.content_type or "application/gzip")
    repo.mark_uploaded(blob.sha256)


@dataclass
class PruneResult:
    candidates: int = 0
    removed: int = 0


def prune_local_blobs(
    session: Session, settings: Settings, *, older_than_days: int, dry_run: bool = False
) -> PruneResult:
    """Drop locally-stored bytes whose blob is confirmed uploaded AND older
    than `older_than_days` — cloud-gated by construction: age alone is
    never enough, only a confirmed `uploaded_at` ever lets a local copy go
    (same rule `app.cloud.sweeper.prune_monitor_bodies` uses, learned from
    the live incident its own docstring describes).
    """
    result = PruneResult()
    if older_than_days <= 0:
        return result
    repo = WebArchiveRepository(session)
    cutoff = datetime.now(UTC) - timedelta(days=older_than_days)
    stmt = select(ArchiveBlob).where(
        ArchiveBlob.uploaded_at.is_not(None),
        ArchiveBlob.local_deleted_at.is_(None),
        ArchiveBlob.first_seen_at < cutoff,
    )
    rows = list(session.scalars(stmt))
    result.candidates = len(rows)
    if dry_run:
        return result
    for blob in rows:
        if storage.delete_blob_file(settings.artifacts_dir, blob.sha256):
            repo.mark_local_deleted(blob.sha256)
            result.removed += 1
    session.commit()
    return result
