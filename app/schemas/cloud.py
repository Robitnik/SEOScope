"""Cloud storage status DTOs (task-52 §3.10). Read-only surface — no key,
no signed URL, no host path ever appears here (PLAN §13/§14).
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class LastBackupDTO(BaseModel):
    key: str
    uploaded_at: datetime | None
    size_bytes: int | None
    status: str


class BackupTierCountsDTO(BaseModel):
    daily: int
    weekly: int
    monthly: int


class CloudQueueDTO(BaseModel):
    pending: int
    failed: int
    uploaded: int


class CloudStatusDTO(BaseModel):
    enabled: bool
    bucket: str | None
    last_backup: LastBackupDTO | None
    backups: BackupTierCountsDTO
    queue: dict[str, CloudQueueDTO]
    archive_since: datetime | None
