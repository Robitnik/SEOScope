"""Import repository (`imported_sources`, PLAN §6/§11): idempotent backfill bookkeeping."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import ImportedSource


class ImportRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def seen(self, sha256: str) -> bool:
        stmt = select(ImportedSource.id).where(ImportedSource.sha256 == sha256)
        return self._session.scalars(stmt).first() is not None

    def record(self, source_path: str, sha256: str, importer_version: str) -> ImportedSource:
        record = ImportedSource(
            source_path=source_path, sha256=sha256, importer_version=importer_version
        )
        self._session.add(record)
        self._session.flush()
        return record
