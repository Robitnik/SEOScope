"""Persistence для інструмента Redirect Checker (task-29).

Як і в task-27, дані тут **мутабельні за задумом**: повторний трейс рядка
перезаписує ланцюжок і вердикт. Тому жодних immutability-гардів, зате явний
`checked_at` — щоб результат був не лише станом, а й фактом у часі.

`claim_target` навмисно зроблений атомарним UPDATE-ом: список проганяє фронт
кількома паралельними запитами, і два кліки по одному рядку не мають дати два
запити до чужого сайту.
"""

from __future__ import annotations

import typing
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import String, cast, delete, func, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from app.db.base import utcnow
from app.db.models import RedirectCheck, RedirectCheckTarget

RUN_STATUSES = ("queued", "running", "done", "failed")


@dataclass(frozen=True)
class TargetInput:
    """Один нормалізований URL, готовий до вставки."""

    row_index: int
    input_url: str
    normalized_url: str
    notes: Sequence[str] = ()


@dataclass(frozen=True)
class TargetFilters:
    query: str | None = None
    verdict: str | None = None
    run_status: str | None = None
    # Один прапорець-сигнал (`temporary_redirect`, `protocol_downgrade`, …).
    flag: str | None = None


@dataclass(frozen=True)
class TargetResultInput:
    verdict: str
    final_url: str | None
    final_status_code: int | None
    hop_count: int
    chain: list[dict[str, Any]]
    flags: list[str]
    final_page: dict[str, Any] | None
    error_code: str | None
    error_detail: str | None
    elapsed_ms: int


class RedirectRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    # --- прогони -------------------------------------------------------------

    def create_check(
        self,
        *,
        client_id: str | None,
        title: str,
        input_count: int,
        duplicate_count: int = 0,
        truncated: bool = False,
        options: dict[str, Any],
        rejected: Sequence[dict[str, str]],
        targets: Iterable[TargetInput],
    ) -> RedirectCheck:
        rows = [
            RedirectCheckTarget(
                row_index=target.row_index,
                input_url=target.input_url,
                normalized_url=target.normalized_url,
                input_notes_json=list(target.notes),
                run_status="queued",
            )
            for target in targets
        ]
        check = RedirectCheck(
            client_id=client_id,
            title=title,
            input_count=input_count,
            target_count=len(rows),
            duplicate_count=duplicate_count,
            truncated=truncated,
            options_json=dict(options),
            rejected_json=[dict(row) for row in rejected],
        )
        self._session.add(check)
        self._session.flush()
        for row in rows:
            row.check_id = check.id
        self._session.add_all(rows)
        self._session.flush()
        return check

    def get_check(self, check_id: str) -> RedirectCheck | None:
        return self._session.get(RedirectCheck, check_id)

    def list_checks(self, *, limit: int, offset: int) -> tuple[list[RedirectCheck], int]:
        total = self._session.scalar(select(func.count()).select_from(RedirectCheck)) or 0
        statement = (
            select(RedirectCheck)
            .order_by(RedirectCheck.created_at.desc(), RedirectCheck.id)
            .limit(limit)
            .offset(offset)
        )
        return list(self._session.scalars(statement)), total

    def delete_check(self, check_id: str) -> bool:
        check = self.get_check(check_id)
        if check is None:
            return False
        # Явне видалення рядків: `ondelete=CASCADE` у SQLite працює лише з
        # увімкненою pragma `foreign_keys`, покладатися на неї тут не треба.
        self._session.execute(
            delete(RedirectCheckTarget).where(RedirectCheckTarget.check_id == check_id)
        )
        self._session.delete(check)
        self._session.flush()
        return True

    def run_status_counts(self, check_id: str) -> dict[str, int]:
        """Прогрес прогону: усі статуси присутні, навіть нульові."""
        counts = dict.fromkeys(RUN_STATUSES, 0)
        statement = (
            select(RedirectCheckTarget.run_status, func.count())
            .where(RedirectCheckTarget.check_id == check_id)
            .group_by(RedirectCheckTarget.run_status)
        )
        for status, count in self._session.execute(statement):
            counts[status] = count
        return counts

    def verdict_counts(self, check_id: str) -> dict[str, int]:
        """Скільки рядків у кожному вердикті. Неперевірені (`None`) не рахуємо."""
        statement = (
            select(RedirectCheckTarget.verdict, func.count())
            .where(
                RedirectCheckTarget.check_id == check_id,
                RedirectCheckTarget.verdict.is_not(None),
            )
            .group_by(RedirectCheckTarget.verdict)
        )
        return {verdict: count for verdict, count in self._session.execute(statement) if verdict}

    # --- рядки ---------------------------------------------------------------

    def list_targets(
        self, check_id: str, *, filters: TargetFilters, limit: int, offset: int
    ) -> tuple[list[RedirectCheckTarget], int]:
        conditions = [RedirectCheckTarget.check_id == check_id]
        if filters.verdict:
            conditions.append(RedirectCheckTarget.verdict == filters.verdict)
        if filters.run_status:
            conditions.append(RedirectCheckTarget.run_status == filters.run_status)
        if filters.flag:
            # Прапорці — короткий список рядків у JSON; підстрічка в лапках
            # відрізняє `host_change` від будь-якого довшого сусіда.
            conditions.append(
                cast(RedirectCheckTarget.flags_json, String).like(f'%"{filters.flag}"%')
            )
        if filters.query:
            needle = f"%{filters.query.strip().lower()}%"
            conditions.append(
                func.lower(RedirectCheckTarget.input_url).like(needle)
                | func.lower(RedirectCheckTarget.normalized_url).like(needle)
                | func.lower(func.coalesce(RedirectCheckTarget.final_url, "")).like(needle)
            )

        total = (
            self._session.scalar(
                select(func.count()).select_from(RedirectCheckTarget).where(*conditions)
            )
            or 0
        )
        statement = (
            select(RedirectCheckTarget)
            .where(*conditions)
            .order_by(RedirectCheckTarget.row_index, RedirectCheckTarget.id)
            .limit(limit)
            .offset(offset)
        )
        return list(self._session.scalars(statement)), total

    def all_targets(self, check_id: str) -> list[RedirectCheckTarget]:
        """Усі рядки прогону в порядку вставки — для експорту."""
        statement = (
            select(RedirectCheckTarget)
            .where(RedirectCheckTarget.check_id == check_id)
            .order_by(RedirectCheckTarget.row_index, RedirectCheckTarget.id)
        )
        return list(self._session.scalars(statement))

    def get_target(self, check_id: str, target_id: str) -> RedirectCheckTarget | None:
        target = self._session.get(RedirectCheckTarget, target_id)
        # Належність прогону: чужий `target_id` — це 404, а не тихе оновлення
        # сусіднього рядка.
        if target is None or target.check_id != check_id:
            return None
        return target

    def claim_target(self, check_id: str, target_id: str) -> RedirectCheckTarget | None:
        """Перевести рядок у `running`, якщо він там ще не є.

        Атомарний UPDATE, а не read-modify-write: подвійний клік або дубльована
        делегація подій інакше дали б два реальні запити до чужого сайту.
        Повертає `None`, якщо рядок уже виконується.
        """
        # `Session.execute` типізований як `Result`, у якого немає `rowcount`;
        # для DML це завжди `CursorResult`, тож звужуємо тип явно.
        statement = (
            update(RedirectCheckTarget)
            .where(
                RedirectCheckTarget.id == target_id,
                RedirectCheckTarget.check_id == check_id,
                RedirectCheckTarget.run_status != "running",
            )
            .values(run_status="running")
        )
        # `cast` тут — typing-івський: у модулі є ще й SQL-ний `sqlalchemy.cast`.
        result = typing.cast("CursorResult[Any]", self._session.execute(statement))
        if not result.rowcount:
            return None
        self._session.flush()
        target = self.get_target(check_id, target_id)
        if target is not None:
            self._session.refresh(target)
        return target

    def release_target(self, target: RedirectCheckTarget, *, error_detail: str) -> None:
        """Рядок не вдалося виконати з причини, що не є станом ланцюжка."""
        target.run_status = "failed"
        target.error_code = "run_failed"
        target.error_detail = error_detail[:1000]
        target.checked_at = utcnow()
        self._touch(target.check_id)
        self._session.flush()

    def save_result(
        self,
        target: RedirectCheckTarget,
        result: TargetResultInput,
        *,
        checked_at: datetime | None = None,
    ) -> RedirectCheckTarget:
        target.run_status = "done"
        target.verdict = result.verdict
        target.final_url = result.final_url
        target.final_status_code = result.final_status_code
        target.hop_count = result.hop_count
        target.chain_json = result.chain
        target.flags_json = result.flags
        target.final_page_json = result.final_page
        target.error_code = result.error_code
        target.error_detail = (result.error_detail or None) and result.error_detail[:1000]
        target.elapsed_ms = result.elapsed_ms
        target.checked_at = checked_at or utcnow()
        self._touch(target.check_id)
        self._session.flush()
        return target

    def _touch(self, check_id: str) -> None:
        check = self.get_check(check_id)
        if check is not None:
            check.updated_at = utcnow()
