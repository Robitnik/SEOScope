"""Persistence для GA4 Events by Month (task-98 §2.3).

Лише `create`/`get`/`list`: рядок незмінний (знімок відповіді GA4), повторний
прогін — новий рядок. Той самий вибір, що в `schema_markup.py`.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import Ga4EventMonthReport


@dataclass(frozen=True)
class Ga4EventReportInput:
    client_id: str
    property_id: str
    months: Sequence[str]
    state: str
    rows: Sequence[dict[str, Any]]
    created_by_user_id: str | None = None
    created_by_kind: str | None = None


class Ga4EventReportRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def create_report(self, data: Ga4EventReportInput) -> Ga4EventMonthReport:
        report = Ga4EventMonthReport(
            client_id=data.client_id,
            property_id=data.property_id,
            months_json=list(data.months),
            state=data.state,
            rows_json=list(data.rows),
            created_by_user_id=data.created_by_user_id,
            created_by_kind=data.created_by_kind,
        )
        self._session.add(report)
        self._session.flush()
        return report

    def get_report(self, report_id: str) -> Ga4EventMonthReport | None:
        return self._session.get(Ga4EventMonthReport, report_id)

    def list_reports(
        self, *, client_id: str | None, limit: int, offset: int
    ) -> tuple[list[Ga4EventMonthReport], int]:
        base = select(Ga4EventMonthReport)
        if client_id is not None:
            base = base.where(Ga4EventMonthReport.client_id == client_id)
        total = self._session.scalar(select(func.count()).select_from(base.subquery())) or 0
        statement = (
            base.order_by(Ga4EventMonthReport.created_at.desc(), Ga4EventMonthReport.id)
            .limit(limit)
            .offset(offset)
        )
        return list(self._session.scalars(statement)), total
