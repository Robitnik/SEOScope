"""Persistence для Schema Markup Checker (task-56).

Той самий вибір, що `content_qa.py`: один незмінний рядок на прогін, без
дитячої таблиці — `findings_json`/`generated_json` не мають per-finding
мутабельного стану. Тому лише `create`/`get`/`list`, без `update`.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import SchemaMarkupCheck


@dataclass(frozen=True)
class SchemaMarkupCheckInput:
    client_id: str | None
    url: str
    final_url: str | None
    http_status: int | None
    verdict: str
    page_kind: str | None
    found_types: Sequence[str]
    findings: Sequence[dict[str, Any]]
    generated: dict[str, Any] | None
    raw_markup: Sequence[dict[str, Any]]
    vocab_version: str
    error: str | None


class SchemaMarkupRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def create_check(self, data: SchemaMarkupCheckInput) -> SchemaMarkupCheck:
        check = SchemaMarkupCheck(
            client_id=data.client_id,
            url=data.url,
            final_url=data.final_url,
            http_status=data.http_status,
            verdict=data.verdict,
            page_kind=data.page_kind,
            found_types_json=list(data.found_types),
            findings_json=list(data.findings),
            generated_json=data.generated,
            raw_markup_json=list(data.raw_markup),
            vocab_version=data.vocab_version,
            error=data.error,
        )
        self._session.add(check)
        self._session.flush()
        return check

    def get_check(self, check_id: str) -> SchemaMarkupCheck | None:
        return self._session.get(SchemaMarkupCheck, check_id)

    def list_checks(
        self, *, client_id: str | None, limit: int, offset: int
    ) -> tuple[list[SchemaMarkupCheck], int]:
        base = select(SchemaMarkupCheck)
        if client_id is not None:
            base = base.where(SchemaMarkupCheck.client_id == client_id)
        total = self._session.scalar(select(func.count()).select_from(base.subquery())) or 0
        statement = (
            base.order_by(SchemaMarkupCheck.created_at.desc(), SchemaMarkupCheck.id)
            .limit(limit)
            .offset(offset)
        )
        return list(self._session.scalars(statement)), total
