"""Persistence для Strategy Package Check (task-90 §4.7).

Один незмінний рядок на пакет (`ContentQaPackage`, `app/db/models.py`) —
той самий принцип, що `ContentQaRepository`: `frameworks_json` не має
per-finding мутабельного стану, тож лише `create`/`get`/`list`.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import ContentQaPackage


@dataclass(frozen=True)
class ContentQaPackageInput:
    client_id: str | None
    source_kind: str  # "gsheet" | "xlsx_blob"
    source_file_id: str | None
    source_name: str
    profile: dict[str, Any]
    framework_count: int
    strategy_findings: Sequence[dict[str, Any]]
    aggregate_findings: Sequence[dict[str, Any]]
    frameworks: Sequence[dict[str, Any]]
    severity_counts: dict[str, int]
    sheets: Sequence[dict[str, Any]] = ()


class ContentQaPackageRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def create_package(self, data: ContentQaPackageInput) -> ContentQaPackage:
        package = ContentQaPackage(
            client_id=data.client_id,
            source_kind=data.source_kind,
            source_file_id=data.source_file_id,
            source_name=data.source_name,
            profile_json=dict(data.profile),
            framework_count=data.framework_count,
            strategy_findings_json=list(data.strategy_findings),
            aggregate_findings_json=list(data.aggregate_findings),
            frameworks_json=list(data.frameworks),
            severity_counts_json=dict(data.severity_counts),
            sheets_json=list(data.sheets),
        )
        self._session.add(package)
        self._session.flush()
        return package

    def get_package(self, package_id: str) -> ContentQaPackage | None:
        return self._session.get(ContentQaPackage, package_id)

    def list_packages(self, *, limit: int, offset: int) -> tuple[list[ContentQaPackage], int]:
        base = select(ContentQaPackage)
        total = self._session.scalar(select(func.count()).select_from(base.subquery())) or 0
        statement = (
            base.order_by(ContentQaPackage.created_at.desc(), ContentQaPackage.id)
            .limit(limit)
            .offset(offset)
        )
        return list(self._session.scalars(statement)), total
