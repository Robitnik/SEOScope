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

from app.authorship.model import Attribution
from app.db.models import ContentQaPackage
from app.db.repositories.reports import apply_authorship_filter


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
    matrix: Sequence[dict[str, Any]] = ()
    # `None` = невідомо (task-93) — той самий інваріант, що решта поля
    # цього класу: якщо викликач не передав автора, ми НЕ вгадуємо "system".
    created_by: Attribution | None = None


class ContentQaPackageRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def create_package(self, data: ContentQaPackageInput) -> ContentQaPackage:
        created_by = data.created_by
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
            matrix_json=list(data.matrix),
            created_by_user_id=created_by.user_id if created_by else None,
            created_by_kind=created_by.kind if created_by else None,
        )
        self._session.add(package)
        self._session.flush()
        return package

    def get_package(self, package_id: str) -> ContentQaPackage | None:
        return self._session.get(ContentQaPackage, package_id)

    def list_packages(
        self,
        *,
        limit: int,
        offset: int,
        created_by_user_id: str | None = None,
        created_by_kind: str | None = None,
    ) -> tuple[list[ContentQaPackage], int]:
        base = apply_authorship_filter(
            select(ContentQaPackage),
            ContentQaPackage,
            created_by_user_id=created_by_user_id,
            created_by_kind=created_by_kind,
        )
        total = self._session.scalar(select(func.count()).select_from(base.subquery())) or 0
        statement = (
            base.order_by(ContentQaPackage.created_at.desc(), ContentQaPackage.id)
            .limit(limit)
            .offset(offset)
        )
        return list(self._session.scalars(statement)), total
