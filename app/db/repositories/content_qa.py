"""Persistence для Strategy Check + Framework Check (task-53).

Один незмінний рядок на прогін: без дитячої таблиці, бо `findings_json` не
має per-finding мутабельного стану (на відміну від `alt_text_items`). Тому
тут немає `update`-методів — лише `create`/`get`/`list`.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import ContentQaReport


@dataclass(frozen=True)
class ContentQaReportInput:
    client_id: str | None
    doc_type: str
    source_filename: str
    profile: dict[str, Any]
    item_count: int
    skipped_sheets: Sequence[str]
    findings: Sequence[dict[str, Any]]
    severity_counts: dict[str, int]
    # Стан аркушів (task-87 §1.3) — `None` для Framework Check (DOCX не має
    # аркушів) і для будь-якого викликача, що ще не постачає його.
    sheets: Sequence[dict[str, Any]] | None = None


class ContentQaRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def create_report(self, data: ContentQaReportInput) -> ContentQaReport:
        report = ContentQaReport(
            client_id=data.client_id,
            doc_type=data.doc_type,
            source_filename=data.source_filename,
            profile_json=dict(data.profile),
            item_count=data.item_count,
            skipped_sheets_json=list(data.skipped_sheets),
            findings_json=list(data.findings),
            severity_counts_json=dict(data.severity_counts),
            sheets_json=list(data.sheets) if data.sheets is not None else None,
        )
        self._session.add(report)
        self._session.flush()
        return report

    def get_report(self, report_id: str) -> ContentQaReport | None:
        return self._session.get(ContentQaReport, report_id)

    def list_reports(
        self, *, doc_type: str, limit: int, offset: int
    ) -> tuple[list[ContentQaReport], int]:
        base = select(ContentQaReport).where(ContentQaReport.doc_type == doc_type)
        total = self._session.scalar(
            select(func.count()).select_from(base.subquery())
        ) or 0
        statement = (
            base.order_by(ContentQaReport.created_at.desc(), ContentQaReport.id)
            .limit(limit)
            .offset(offset)
        )
        return list(self._session.scalars(statement)), total
