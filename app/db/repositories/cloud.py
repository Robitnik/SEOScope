"""Cloud storage ledger repository (task-52 §3.3): the only place that
touches `cloud_objects` directly.

Three call shapes:

- **candidates**: `monitor_blob_candidates`/`report_file_candidates` LEFT-JOIN
  their source table against `cloud_objects` in one query — a row with no
  `uploaded` entry yet (never tried, or previously `failed`) is a candidate.
  This is `app/cloud/sweeper.py`'s entire "what's left to upload" query.
- **writes**: `record_uploaded`/`record_failed` upsert by `(kind,
  source_ref)` — the table's `UNIQUE` constraint is what makes a re-run
  idempotent instead of creating duplicate ledger rows (and duplicate B2
  uploads) for the same source object.
- **status**: `kind_counts`/`*_total` back `GET /api/v1/cloud/status`
  (`app/api/cloud.py`) — `pending` there is `total - uploaded - failed`,
  never a persisted status: this sweeper design never leaves a row sitting
  at an intermediate "pending" state (see module docstring in
  `app/cloud/sweeper.py` for why).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.base import utcnow
from app.db.models import CloudObject, MonitorBlob, ReportFile


class CloudLedgerRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def commit(self) -> None:
        self._session.commit()

    # --- single-object lookups --------------------------------------------

    def get(self, kind: str, source_ref: str) -> CloudObject | None:
        return self._session.scalar(
            select(CloudObject).where(
                CloudObject.kind == kind, CloudObject.source_ref == source_ref
            )
        )

    def is_uploaded(self, kind: str, source_ref: str) -> bool:
        obj = self.get(kind, source_ref)
        return obj is not None and obj.status == "uploaded"

    # --- ledger writes ------------------------------------------------------

    def _upsert(self, kind: str, source_ref: str) -> CloudObject:
        existing = self.get(kind, source_ref)
        if existing is not None:
            return existing
        obj = CloudObject(kind=kind, source_ref=source_ref, key="")
        self._session.add(obj)
        self._session.flush()
        return obj

    def record_uploaded(
        self, *, kind: str, source_ref: str, key: str, sha256: str | None, size_bytes: int | None
    ) -> CloudObject:
        obj = self._upsert(kind, source_ref)
        obj.key = key
        obj.sha256 = sha256
        obj.size_bytes = size_bytes
        obj.status = "uploaded"
        obj.error_message = None
        obj.attempts += 1
        obj.uploaded_at = utcnow()
        self._session.flush()
        return obj

    def record_failed(
        self, *, kind: str, source_ref: str, key: str, error_message: str
    ) -> CloudObject:
        obj = self._upsert(kind, source_ref)
        obj.key = key
        obj.status = "failed"
        obj.error_message = error_message[:1000]
        obj.attempts += 1
        self._session.flush()
        return obj

    # --- sweep candidates -----------------------------------------------------

    def monitor_blob_candidates(self, *, since: datetime, limit: int) -> list[MonitorBlob]:
        uploaded_refs = select(CloudObject.source_ref).where(
            CloudObject.kind == "monitor_blob", CloudObject.status == "uploaded"
        )
        stmt = (
            select(MonitorBlob)
            .where(MonitorBlob.first_seen_at >= since)
            .where(MonitorBlob.sha256.not_in(uploaded_refs))
            .order_by(MonitorBlob.first_seen_at)
            .limit(limit)
        )
        return list(self._session.scalars(stmt))

    def report_file_key_taken_by_other(self, key: str, *, exclude_source_ref: str) -> bool:
        """True if some OTHER report_file already owns this human-readable
        key (task-52 §3.7 name-collision guard) — the sweeper appends a
        sha256-prefix suffix and retries under a new key when this is True.
        """
        stmt = select(func.count()).select_from(CloudObject).where(
            CloudObject.kind == "report_file",
            CloudObject.key == key,
            CloudObject.status == "uploaded",
            CloudObject.source_ref != exclude_source_ref,
        )
        return (self._session.scalar(stmt) or 0) > 0

    def report_file_candidates(self, *, limit: int) -> list[ReportFile]:
        uploaded_refs = select(CloudObject.source_ref).where(
            CloudObject.kind == "report_file", CloudObject.status == "uploaded"
        )
        stmt = (
            select(ReportFile)
            .where(ReportFile.id.not_in(uploaded_refs))
            .order_by(ReportFile.created_at)
            .limit(limit)
        )
        return list(self._session.scalars(stmt))

    # --- status counts ----------------------------------------------------

    def kind_counts(self, kind: str) -> dict[str, int]:
        uploaded = (
            self._session.scalar(
                select(func.count())
                .select_from(CloudObject)
                .where(CloudObject.kind == kind, CloudObject.status == "uploaded")
            )
            or 0
        )
        failed = (
            self._session.scalar(
                select(func.count())
                .select_from(CloudObject)
                .where(CloudObject.kind == kind, CloudObject.status == "failed")
            )
            or 0
        )
        return {"uploaded": uploaded, "failed": failed}

    def monitor_blob_total(self, *, since: datetime) -> int:
        return (
            self._session.scalar(
                select(func.count())
                .select_from(MonitorBlob)
                .where(MonitorBlob.first_seen_at >= since)
            )
            or 0
        )

    def report_file_total(self) -> int:
        return self._session.scalar(select(func.count()).select_from(ReportFile)) or 0

    # --- watermark ("archive only what's new", task-52 §3.4) -----------------

    def get_watermark(self, name: str) -> datetime | None:
        row = self.get("watermark", name)
        if row is None or not row.key:
            return None
        return datetime.fromisoformat(row.key)

    def set_watermark(self, name: str, value: datetime) -> None:
        row = self._upsert("watermark", name)
        row.key = value.isoformat()
        row.status = "uploaded"
        self._session.flush()

    def get_or_init_watermark(self, name: str, *, default: datetime) -> datetime:
        """First activation fixes the cutover moment permanently — every
        later tick reads the same value back instead of re-deriving "now".
        """
        existing = self.get_watermark(name)
        if existing is not None:
            return existing
        self.set_watermark(name, default)
        return default

    # --- db backups (GFS tiers, task-52 §3.5) ------------------------------

    def record_backup(
        self, *, tier: str, date_key: str, key: str, sha256: str, size_bytes: int
    ) -> CloudObject:
        return self.record_uploaded(
            kind="db_backup",
            source_ref=f"{tier}/{date_key}",
            key=key,
            sha256=sha256,
            size_bytes=size_bytes,
        )

    def has_backup(self, *, tier: str, date_key: str) -> bool:
        return self.is_uploaded("db_backup", f"{tier}/{date_key}")

    def backups_by_tier(self, tier: str) -> list[CloudObject]:
        """Newest first. Lexicographic `source_ref` sort is safe because the
        date suffix is always zero-padded ISO (`YYYY-MM-DD`)."""
        stmt = (
            select(CloudObject)
            .where(
                CloudObject.kind == "db_backup",
                CloudObject.source_ref.like(f"{tier}/%"),
                CloudObject.status == "uploaded",
            )
            .order_by(CloudObject.source_ref.desc())
        )
        return list(self._session.scalars(stmt))

    def latest_backup(self) -> CloudObject | None:
        stmt = (
            select(CloudObject)
            .where(CloudObject.kind == "db_backup", CloudObject.status == "uploaded")
            .order_by(CloudObject.uploaded_at.desc())
            .limit(1)
        )
        return self._session.scalar(stmt)

    def delete_backup_row(self, obj: CloudObject) -> None:
        self._session.delete(obj)
