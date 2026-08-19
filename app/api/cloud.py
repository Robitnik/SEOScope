"""Cloud storage status API (task-52 §3.10), prefix `/api/v1`.

Read-only: this router never uploads, deletes or authorizes against B2 — it
only reads `cloud_objects` (the sweeper/backup jobs run inside the worker's
`CloudScheduler`). Same sync-repository-via-threadpool pattern as
`app/api/monitor.py`.
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.concurrency import run_in_threadpool

from app.api import register_router
from app.cloud.sweeper import ARCHIVE_WATERMARK
from app.core.config import get_settings
from app.db.repositories.cloud import CloudLedgerRepository
from app.db.session import worker_session_scope
from app.schemas.cloud import (
    BackupTierCountsDTO,
    CloudQueueDTO,
    CloudStatusDTO,
    LastBackupDTO,
)

router = APIRouter(prefix="/api/v1", tags=["cloud"])

_MONITOR_BLOB = "monitor_blob"
_REPORT_FILE = "report_file"


def _status() -> CloudStatusDTO:
    settings = get_settings()
    with worker_session_scope() as session:
        ledger = CloudLedgerRepository(session)

        last_backup_row = ledger.latest_backup()
        last_backup = (
            LastBackupDTO(
                key=last_backup_row.key,
                uploaded_at=last_backup_row.uploaded_at,
                size_bytes=last_backup_row.size_bytes,
                status=last_backup_row.status,
            )
            if last_backup_row is not None
            else None
        )

        backups = BackupTierCountsDTO(
            daily=len(ledger.backups_by_tier("daily")),
            weekly=len(ledger.backups_by_tier("weekly")),
            monthly=len(ledger.backups_by_tier("monthly")),
        )

        archive_since = ledger.get_watermark(ARCHIVE_WATERMARK)

        queue: dict[str, CloudQueueDTO] = {}
        for kind, total in (
            (_MONITOR_BLOB, ledger.monitor_blob_total(since=archive_since) if archive_since else 0),
            (_REPORT_FILE, ledger.report_file_total()),
        ):
            counts = ledger.kind_counts(kind)
            pending = max(0, total - counts["uploaded"] - counts["failed"])
            queue[kind] = CloudQueueDTO(
                pending=pending, failed=counts["failed"], uploaded=counts["uploaded"]
            )

    return CloudStatusDTO(
        enabled=settings.cloud_enabled,
        bucket=settings.b2_vault_bucket_name or None,
        last_backup=last_backup,
        backups=backups,
        queue=queue,
        archive_since=archive_since,
    )


@router.get("/cloud/status", response_model=CloudStatusDTO)
async def get_cloud_status() -> CloudStatusDTO:
    return await run_in_threadpool(_status)


register_router(router)
