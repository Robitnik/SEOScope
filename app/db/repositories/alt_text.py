"""Persistence для інструмента Image Alt Text (task-27).

На відміну від решти репозиторіїв тут дані **мутабельні за задумом**: статус
прогресу («alt скопійовано») і результат перевірки сторінки змінюються щоразу,
коли оператор працює з рядком. Тому жодних immutability-гардів, зате явний
`status_changed_at`/`verify_checked_at` — щоб прогрес був не лише станом, а й
фактом у часі.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.db.base import utcnow
from app.db.models import AltTextItem, AltTextReport

# Статуси прогресу. `copied` ставить копіювання alt; `applied` — лише явне
# підтвердження або вердикт `match` (ніколи здогадкою).
ITEM_STATUSES = ("pending", "copied", "applied", "skipped")


@dataclass(frozen=True)
class AltTextItemInput:
    """Один розпарсений і дерівований рядок, готовий до вставки."""

    row_index: int
    image_url: str
    search_key: str
    search_key_fallback: str | None
    url_kind: str
    derivation: dict[str, Any]
    new_alt: str
    old_alt: str | None
    # Усі сторінки, де картинка використовується (перша йде і в `page_url`).
    page_urls: list[str]
    extra: dict[str, str]


@dataclass(frozen=True)
class ItemFilters:
    query: str | None = None
    status: str | None = None
    url_kind: str | None = None
    verify_status: str | None = None
    # `has_page=True` — лише рядки, які взагалі можна перевірити.
    has_page: bool | None = None


class AltTextRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    # --- звіти ---------------------------------------------------------------

    def create_report(
        self,
        *,
        client_id: str | None,
        title: str,
        source_filename: str,
        source_kind: str,
        source_sheet: str | None,
        source_sha256: str,
        column_map: dict[str, int],
        skipped_count: int,
        warnings: Sequence[str],
        items: Iterable[AltTextItemInput],
    ) -> AltTextReport:
        rows = [
            AltTextItem(
                row_index=item.row_index,
                image_url=item.image_url,
                search_key=item.search_key,
                search_key_fallback=item.search_key_fallback,
                url_kind=item.url_kind,
                derivation_json=item.derivation,
                new_alt=item.new_alt,
                old_alt=item.old_alt,
                page_url=item.page_urls[0] if item.page_urls else None,
                page_urls_json=list(item.page_urls),
                extra_json=item.extra,
                status="pending",
            )
            for item in items
        ]
        report = AltTextReport(
            client_id=client_id,
            title=title,
            source_filename=source_filename,
            source_kind=source_kind,
            source_sheet=source_sheet,
            source_sha256=source_sha256,
            column_map_json=dict(column_map),
            row_count=len(rows),
            skipped_count=skipped_count,
            warnings_json=list(warnings),
        )
        self._session.add(report)
        self._session.flush()
        for row in rows:
            row.report_id = report.id
        self._session.add_all(rows)
        self._session.flush()
        return report

    def get_report(self, report_id: str) -> AltTextReport | None:
        return self._session.get(AltTextReport, report_id)

    def find_by_source(self, source_sha256: str, sheet: str | None) -> AltTextReport | None:
        """Той самий файл і та сама вкладка вже завантажені?"""
        statement = select(AltTextReport).where(AltTextReport.source_sha256 == source_sha256)
        statement = statement.where(
            AltTextReport.source_sheet.is_(None)
            if sheet is None
            else AltTextReport.source_sheet == sheet
        )
        return self._session.scalars(statement.order_by(AltTextReport.created_at.desc())).first()

    def list_reports(self, *, limit: int, offset: int) -> tuple[list[AltTextReport], int]:
        total = self._session.scalar(select(func.count()).select_from(AltTextReport)) or 0
        statement = (
            select(AltTextReport)
            .order_by(AltTextReport.created_at.desc(), AltTextReport.id)
            .limit(limit)
            .offset(offset)
        )
        return list(self._session.scalars(statement)), total

    def delete_report(self, report_id: str) -> bool:
        report = self.get_report(report_id)
        if report is None:
            return False
        # Явне видалення рядків: `ondelete=CASCADE` у SQLite працює лише при
        # увімкнених foreign_keys, а покладатися на pragma тут нема потреби.
        self._session.execute(delete(AltTextItem).where(AltTextItem.report_id == report_id))
        self._session.delete(report)
        self._session.flush()
        return True

    def status_counts(self, report_id: str) -> dict[str, int]:
        """Лічильники прогресу: усі статуси присутні, навіть нульові."""
        counts = dict.fromkeys(ITEM_STATUSES, 0)
        statement = (
            select(AltTextItem.status, func.count())
            .where(AltTextItem.report_id == report_id)
            .group_by(AltTextItem.status)
        )
        for status, count in self._session.execute(statement):
            counts[status] = count
        return counts

    def verify_counts(self, report_id: str) -> dict[str, int]:
        """Скільки рядків у кожному вердикті. `None` (не перевіряли) не рахуємо."""
        statement = (
            select(AltTextItem.verify_status, func.count())
            .where(AltTextItem.report_id == report_id, AltTextItem.verify_status.is_not(None))
            .group_by(AltTextItem.verify_status)
        )
        return {status: count for status, count in self._session.execute(statement) if status}

    def verifiable_count(self, report_id: str) -> int:
        return (
            self._session.scalar(
                select(func.count())
                .select_from(AltTextItem)
                .where(AltTextItem.report_id == report_id, AltTextItem.page_url.is_not(None))
            )
            or 0
        )

    # --- рядки ---------------------------------------------------------------

    def list_items(
        self, report_id: str, *, filters: ItemFilters, limit: int, offset: int
    ) -> tuple[list[AltTextItem], int]:
        conditions = [AltTextItem.report_id == report_id]
        if filters.status:
            conditions.append(AltTextItem.status == filters.status)
        if filters.url_kind:
            conditions.append(AltTextItem.url_kind == filters.url_kind)
        if filters.verify_status:
            conditions.append(AltTextItem.verify_status == filters.verify_status)
        if filters.has_page is True:
            conditions.append(AltTextItem.page_url.is_not(None))
        elif filters.has_page is False:
            conditions.append(AltTextItem.page_url.is_(None))
        if filters.query:
            needle = f"%{filters.query.strip().lower()}%"
            conditions.append(
                func.lower(AltTextItem.image_url).like(needle)
                | func.lower(AltTextItem.new_alt).like(needle)
                | func.lower(AltTextItem.search_key).like(needle)
            )

        total = (
            self._session.scalar(select(func.count()).select_from(AltTextItem).where(*conditions))
            or 0
        )
        statement = (
            select(AltTextItem)
            .where(*conditions)
            .order_by(AltTextItem.row_index, AltTextItem.id)
            .limit(limit)
            .offset(offset)
        )
        return list(self._session.scalars(statement)), total

    def all_items(self, report_id: str) -> list[AltTextItem]:
        """Усі рядки звіту в порядку джерела — для експорту."""
        statement = (
            select(AltTextItem)
            .where(AltTextItem.report_id == report_id)
            .order_by(AltTextItem.row_index, AltTextItem.id)
        )
        return list(self._session.scalars(statement))

    def get_item(self, report_id: str, item_id: str) -> AltTextItem | None:
        item = self._session.get(AltTextItem, item_id)
        # Перевірка належності звіту: `item_id` із чужого звіту — це 404, а не
        # тихе оновлення сусіднього рядка.
        if item is None or item.report_id != report_id:
            return None
        return item

    def set_status(self, item: AltTextItem, status: str) -> AltTextItem:
        if status not in ITEM_STATUSES:
            raise ValueError(f"unknown status: {status}")
        item.status = status
        item.status_changed_at = None if status == "pending" else utcnow()
        self._touch(item.report_id)
        self._session.flush()
        return item

    def set_verify_result(
        self,
        item: AltTextItem,
        *,
        status: str,
        found_alt: str | None,
        http_status: int | None,
        detail: str | None,
        checked_at: datetime | None = None,
    ) -> AltTextItem:
        item.verify_status = status
        item.verify_alt = found_alt
        item.verify_http_status = http_status
        item.verify_detail = detail
        item.verify_checked_at = checked_at or utcnow()
        # Підтверджений збіг — єдина підстава підняти статус автоматично.
        if status == "match" and item.status != "applied":
            item.status = "applied"
            item.status_changed_at = item.verify_checked_at
        self._touch(item.report_id)
        self._session.flush()
        return item

    def _touch(self, report_id: str) -> None:
        report = self.get_report(report_id)
        if report is not None:
            report.updated_at = utcnow()
