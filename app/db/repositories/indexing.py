"""Репозиторій Indexing Planner (task-100 §2.2) — єдине місце, що торкається
`indexing_*` таблиць напряму.

Три правила, які тут тримаються всім файлом:

1. **`is_indexed IS NULL` ніколи не читається як `False`.** Кандидатом URL
   стає лише після того, як його справді проінспектували. Інакше
   непросканований портфель виглядав би як 2311 кандидатів замість 274
   реальних (заміряно, план §1.5).
2. **Порожньо ≠ нуль.** Лічильники клієнта, якого жодного разу не
   сканували, повертаються як `None` (PLAN §12).
3. **Сьогоднішній план недоторканний.** `replace_future_plan` фізично не
   може видалити рядок зі `planned_for <= today` — це не перевірка в
   викличнику, а умова в самому `DELETE`.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import Select, and_, func, or_, select
from sqlalchemy.orm import Session

from app.db.base import utcnow
from app.db.models import (
    Client,
    ClientStatus,
    IndexingScan,
    IndexingSettings,
    IndexingSubmission,
    IndexingUrl,
)

# Вікно, протягом якого помилковий `Mark sent` ще можна відкликати.
UNDO_WINDOW = timedelta(hours=24)
# Кандидат — це URL, який ІНСПЕКТУВАЛИ і який не в індексі.
_IS_CANDIDATE = and_(IndexingUrl.inspected_at.is_not(None), IndexingUrl.is_indexed.is_(False))


class IndexingRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def commit(self) -> None:
        self._session.commit()

    def flush(self) -> None:
        self._session.flush()

    # --- налаштування -----------------------------------------------------

    def get_or_create_settings(self, client_id: str) -> IndexingSettings:
        row = self._session.scalar(
            select(IndexingSettings).where(IndexingSettings.client_id == client_id)
        )
        if row is None:
            row = IndexingSettings(client_id=client_id)
            self._session.add(row)
            self._session.flush()
        return row

    def settings_map(self) -> dict[str, IndexingSettings]:
        rows = self._session.scalars(select(IndexingSettings)).all()
        return {row.client_id: row for row in rows}

    def enabled_client_ids(self) -> list[str]:
        return list(
            self._session.scalars(
                select(IndexingSettings.client_id).where(IndexingSettings.enabled.is_(True))
            ).all()
        )

    def active_clients(self) -> list[Client]:
        return list(
            self._session.scalars(
                select(Client)
                .where(Client.status == ClientStatus.active)
                .order_by(Client.name)
            ).all()
        )

    def client_names(self) -> dict[str, str]:
        return {
            row[0]: row[1] for row in self._session.execute(select(Client.id, Client.name)).all()
        }

    # --- реєстр URL -------------------------------------------------------

    def get_url(self, url_id: str) -> IndexingUrl | None:
        return self._session.get(IndexingUrl, url_id)

    def urls_by_key(self, client_id: str) -> dict[str, IndexingUrl]:
        rows = self._session.scalars(
            select(IndexingUrl).where(IndexingUrl.client_id == client_id)
        ).all()
        return {row.url_key: row for row in rows}

    def add_url(
        self, *, client_id: str, url: str, url_key: str, lastmod: date | None, seen_at: datetime
    ) -> IndexingUrl:
        row = IndexingUrl(
            client_id=client_id,
            url=url,
            url_key=url_key,
            sitemap_lastmod=lastmod,
            first_seen_at=seen_at,
            last_seen_in_sitemap_at=seen_at,
        )
        self._session.add(row)
        return row

    def record_inspection(
        self,
        row: IndexingUrl,
        *,
        fields: dict[str, Any],
        is_indexed: bool | None,
        now: datetime | None = None,
    ) -> None:
        """Записати зріз URL Inspection і — окремо — переходи індексу.

        `first_indexed_seen_at`/`left_index_at` ставляться ТУТ, а не в
        аналітиці: перехід видно лише в момент, коли новий стан
        порівнюється зі старим, і відновити його заднім числом з одного
        лише поточного стану неможливо.
        """
        moment = now or utcnow()
        was_indexed = row.is_indexed
        for key, value in fields.items():
            setattr(row, key, value)
        row.inspected_at = moment
        row.is_indexed = is_indexed
        if is_indexed is True:
            if row.first_indexed_seen_at is None:
                row.first_indexed_seen_at = moment
            row.left_index_at = None
        elif is_indexed is False and was_indexed is True:
            row.left_index_at = moment

    def urls_for_client(self, client_id: str) -> list[IndexingUrl]:
        return list(
            self._session.scalars(
                select(IndexingUrl).where(IndexingUrl.client_id == client_id)
            ).all()
        )

    def recently_submitted_url_ids(self, client_id: str, *, since: datetime) -> set[str]:
        rows = self._session.scalars(
            select(IndexingSubmission.indexing_url_id).where(
                IndexingSubmission.client_id == client_id,
                IndexingSubmission.status == "sent",
                IndexingSubmission.submitted_at >= since,
            )
        ).all()
        return set(rows)

    # --- лічильники -------------------------------------------------------

    def client_counts(self, client_id: str) -> dict[str, int]:
        row = self._session.execute(
            select(
                func.count(IndexingUrl.id),
                func.count(IndexingUrl.id).filter(IndexingUrl.is_indexed.is_(True)),
                func.count(IndexingUrl.id).filter(
                    _IS_CANDIDATE, IndexingUrl.excluded.is_(False)
                ),
                func.count(IndexingUrl.id).filter(IndexingUrl.excluded.is_(True)),
                func.count(IndexingUrl.id).filter(IndexingUrl.inspected_at.is_(None)),
            ).where(
                IndexingUrl.client_id == client_id,
                IndexingUrl.gone_from_sitemap_at.is_(None),
            )
        ).one()
        return {
            "urls_total": row[0],
            "indexed": row[1],
            "candidates": row[2],
            "excluded": row[3],
            "never_inspected": row[4],
        }

    def portfolio_counts(self, client_ids: list[str]) -> dict[str, int]:
        if not client_ids:
            return {
                "urls_total": 0,
                "indexed": 0,
                "candidates": 0,
                "excluded": 0,
                "never_inspected": 0,
            }
        row = self._session.execute(
            select(
                func.count(IndexingUrl.id),
                func.count(IndexingUrl.id).filter(IndexingUrl.is_indexed.is_(True)),
                func.count(IndexingUrl.id).filter(
                    _IS_CANDIDATE, IndexingUrl.excluded.is_(False)
                ),
                func.count(IndexingUrl.id).filter(IndexingUrl.excluded.is_(True)),
                func.count(IndexingUrl.id).filter(IndexingUrl.inspected_at.is_(None)),
            ).where(
                IndexingUrl.client_id.in_(client_ids),
                IndexingUrl.gone_from_sitemap_at.is_(None),
            )
        ).one()
        return {
            "urls_total": row[0],
            "indexed": row[1],
            "candidates": row[2],
            "excluded": row[3],
            "never_inspected": row[4],
        }

    # --- черга ------------------------------------------------------------

    def _queue_stmt(
        self,
        *,
        client_ids: list[str] | None,
        reason_code: str | None,
        excluded: bool,
    ) -> Select[tuple[IndexingUrl]]:
        stmt = select(IndexingUrl).where(IndexingUrl.gone_from_sitemap_at.is_(None))
        if excluded:
            stmt = stmt.where(IndexingUrl.excluded.is_(True))
        else:
            stmt = stmt.where(IndexingUrl.excluded.is_(False), _IS_CANDIDATE)
        if client_ids is not None:
            stmt = stmt.where(IndexingUrl.client_id.in_(client_ids))
        if reason_code:
            stmt = stmt.where(IndexingUrl.reason_code == reason_code)
        return stmt

    def queue(
        self,
        *,
        client_ids: list[str] | None = None,
        reason_code: str | None = None,
        excluded: bool = False,
        offset: int = 0,
        limit: int = 100,
    ) -> tuple[list[IndexingUrl], int]:
        stmt = self._queue_stmt(
            client_ids=client_ids, reason_code=reason_code, excluded=excluded
        )
        total = self._session.scalar(
            select(func.count()).select_from(stmt.subquery())
        )
        rows = self._session.scalars(
            stmt.order_by(
                IndexingUrl.priority_score.desc().nullslast(), IndexingUrl.url_key
            )
            .offset(offset)
            .limit(limit)
        ).all()
        return list(rows), int(total or 0)

    def queue_size(self, client_id: str) -> int:
        return int(
            self._session.scalar(
                select(func.count(IndexingUrl.id)).where(
                    IndexingUrl.client_id == client_id,
                    IndexingUrl.gone_from_sitemap_at.is_(None),
                    IndexingUrl.excluded.is_(False),
                    _IS_CANDIDATE,
                )
            )
            or 0
        )

    def candidates_for_plan(self, client_id: str, *, limit: int) -> list[IndexingUrl]:
        return list(
            self._session.scalars(
                select(IndexingUrl)
                .where(
                    IndexingUrl.client_id == client_id,
                    IndexingUrl.gone_from_sitemap_at.is_(None),
                    IndexingUrl.excluded.is_(False),
                    _IS_CANDIDATE,
                    IndexingUrl.priority_score.is_not(None),
                )
                .order_by(IndexingUrl.priority_score.desc(), IndexingUrl.url_key)
                .limit(limit)
            ).all()
        )

    # --- план і відправки -------------------------------------------------

    def submissions_for_day(
        self, day: date, *, client_ids: list[str] | None = None
    ) -> list[IndexingSubmission]:
        stmt = select(IndexingSubmission).where(IndexingSubmission.planned_for == day)
        if client_ids is not None:
            stmt = stmt.where(IndexingSubmission.client_id.in_(client_ids))
        return list(self._session.scalars(stmt.order_by(IndexingSubmission.created_at)).all())

    def submissions_in_range(
        self, start: date, end: date, *, client_ids: list[str] | None = None
    ) -> list[IndexingSubmission]:
        stmt = select(IndexingSubmission).where(
            IndexingSubmission.planned_for >= start, IndexingSubmission.planned_for <= end
        )
        if client_ids is not None:
            stmt = stmt.where(IndexingSubmission.client_id.in_(client_ids))
        return list(self._session.scalars(stmt).all())

    def get_submission(self, submission_id: str) -> IndexingSubmission | None:
        return self._session.get(IndexingSubmission, submission_id)

    def submissions_for_url(self, url_id: str) -> list[IndexingSubmission]:
        return list(
            self._session.scalars(
                select(IndexingSubmission)
                .where(IndexingSubmission.indexing_url_id == url_id)
                .order_by(IndexingSubmission.planned_for.desc())
            ).all()
        )

    def planned_url_ids(self, client_id: str, *, on_or_after: date) -> set[str]:
        """URL, які вже стоять у плані — щоб той самий не потрапив двічі."""
        rows = self._session.scalars(
            select(IndexingSubmission.indexing_url_id).where(
                IndexingSubmission.client_id == client_id,
                IndexingSubmission.planned_for >= on_or_after,
            )
        ).all()
        return set(rows)

    def replace_future_plan(self, client_id: str, *, today: date) -> int:
        """Прибрати ЩЕ НЕ ЧІПАНІ майбутні клітинки перед переплануванням.

        Умова `planned_for > today` стоїть у самому запиті, а не в
        викличнику: сьогоднішній день заморожений, і жоден шлях коду не
        має вміти витягти з-під оператора URL, який він уже почав нести в
        GSC (план §2.3, крок 4). `status == "planned"` бережно лишає
        `sent`/`quota_blocked` навіть у майбутньому.
        """
        rows = self._session.scalars(
            select(IndexingSubmission).where(
                IndexingSubmission.client_id == client_id,
                IndexingSubmission.planned_for > today,
                IndexingSubmission.status == "planned",
            )
        ).all()
        for row in rows:
            self._session.delete(row)
        return len(rows)

    def add_submission(
        self,
        *,
        client_id: str,
        url: IndexingUrl,
        planned_for: date,
        reason_code: str,
        reason_json: dict[str, Any],
    ) -> IndexingSubmission:
        row = IndexingSubmission(
            client_id=client_id,
            indexing_url_id=url.id,
            planned_for=planned_for,
            status="planned",
            reason_code=reason_code,
            reason_json=reason_json,
            state_at_plan=url.coverage_state,
        )
        self._session.add(row)
        return row

    def mark_sent(
        self, submission: IndexingSubmission, *, user_id: str | None, now: datetime | None = None
    ) -> bool:
        """Ідемпотентно. Повертає `False`, якщо вже було відмічено."""
        if submission.status == "sent":
            return False
        moment = now or utcnow()
        url = self._session.get(IndexingUrl, submission.indexing_url_id)
        submission.status = "sent"
        submission.submitted_at = moment
        submission.submitted_by_user_id = user_id
        if url is not None:
            submission.state_at_submission = url.coverage_state
            url.last_submitted_at = moment
            url.submissions_count = (url.submissions_count or 0) + 1
        return True

    def mark_quota_blocked(
        self, submission: IndexingSubmission, *, now: datetime | None = None
    ) -> bool:
        """GSC відмовив по квоті — це НЕ відправка.

        Cooldown не ставиться і `last_submitted_at` не рухається: інакше
        сторінка, якої ніхто не подав, зникла б на 90 днів (план §2.7 п.1).
        """
        if submission.status == "quota_blocked":
            return False
        submission.status = "quota_blocked"
        submission.submitted_at = None
        submission.state_at_submission = None
        return True

    def undo_submission(self, submission: IndexingSubmission) -> bool:
        """Відкотити помилкову відмітку разом із її слідом на URL."""
        if submission.status != "sent" or submission.submitted_at is None:
            return False
        if utcnow() - submission.submitted_at > UNDO_WINDOW:
            return False
        url = self._session.get(IndexingUrl, submission.indexing_url_id)
        if url is not None:
            url.submissions_count = max((url.submissions_count or 1) - 1, 0)
            previous = self._session.scalar(
                select(func.max(IndexingSubmission.submitted_at)).where(
                    IndexingSubmission.indexing_url_id == url.id,
                    IndexingSubmission.status == "sent",
                    IndexingSubmission.id != submission.id,
                )
            )
            url.last_submitted_at = previous
        submission.status = "planned"
        submission.submitted_at = None
        submission.submitted_by_user_id = None
        submission.state_at_submission = None
        return True

    def close_indexed_submissions(self, url: IndexingUrl, *, indexed_at: datetime) -> int:
        """Проставити `indexed_at` найсвіжішій незакритій відправці URL."""
        row = self._session.scalar(
            select(IndexingSubmission)
            .where(
                IndexingSubmission.indexing_url_id == url.id,
                IndexingSubmission.status == "sent",
                IndexingSubmission.indexed_at.is_(None),
            )
            .order_by(IndexingSubmission.submitted_at.desc())
            .limit(1)
        )
        if row is None or row.submitted_at is None:
            return 0
        row.indexed_at = indexed_at
        row.days_to_index = max((indexed_at - row.submitted_at).days, 0)
        return 1

    def sent_count(self, *, client_id: str | None = None, since: datetime) -> int:
        stmt = select(func.count(IndexingSubmission.id)).where(
            IndexingSubmission.status == "sent", IndexingSubmission.submitted_at >= since
        )
        if client_id is not None:
            stmt = stmt.where(IndexingSubmission.client_id == client_id)
        return int(self._session.scalar(stmt) or 0)

    # --- скани ------------------------------------------------------------

    def start_scan(
        self,
        *,
        client_id: str | None,
        trigger: str,
        owner: str | None = None,
        user_id: str | None = None,
    ) -> IndexingScan:
        row = IndexingScan(
            client_id=client_id,
            trigger=trigger,
            owner=owner,
            heartbeat_at=utcnow(),
            created_by_user_id=user_id,
        )
        self._session.add(row)
        self._session.flush()
        return row

    def get_scan(self, scan_id: str) -> IndexingScan | None:
        return self._session.get(IndexingScan, scan_id)

    def last_scan(self, *, client_id: str | None = None) -> IndexingScan | None:
        stmt = select(IndexingScan)
        if client_id is not None:
            stmt = stmt.where(IndexingScan.client_id == client_id)
        return self._session.scalar(stmt.order_by(IndexingScan.started_at.desc()).limit(1))

    def last_scan_per_client(self) -> dict[str, IndexingScan]:
        rows = self._session.scalars(
            select(IndexingScan)
            .where(IndexingScan.client_id.is_not(None))
            .order_by(IndexingScan.started_at)
        ).all()
        out: dict[str, IndexingScan] = {}
        for row in rows:
            if row.client_id is not None:
                out[row.client_id] = row
        return out

    def has_successful_scan_since(self, moment: datetime) -> bool:
        """Ідемпотентність доби: чи вже був успішний портфельний прогін."""
        found = self._session.scalar(
            select(IndexingScan.id)
            .where(
                IndexingScan.client_id.is_(None),
                IndexingScan.started_at >= moment,
                IndexingScan.status.in_(("ok", "partial")),
            )
            .limit(1)
        )
        return found is not None

    def running_portfolio_scan(self, *, stale_after: timedelta) -> IndexingScan | None:
        """Живий портфельний прогін — щоб другий не стартував паралельно.

        «Живий» визначається `heartbeat_at`, а не пам'яттю потоку: CLI-прогін
        у власному процесі для неї невидимий (урок task-62).
        """
        cutoff = utcnow() - stale_after
        return self._session.scalar(
            select(IndexingScan)
            .where(
                IndexingScan.client_id.is_(None),
                IndexingScan.status == "running",
                IndexingScan.heartbeat_at.is_not(None),
                IndexingScan.heartbeat_at >= cutoff,
            )
            .limit(1)
        )

    # --- аналітика --------------------------------------------------------

    def lag_samples(self, *, since: datetime, client_id: str | None = None) -> list[int]:
        """Завершені спостереження «відправили -> зайшло»."""
        stmt = select(IndexingSubmission.days_to_index).where(
            IndexingSubmission.status == "sent",
            IndexingSubmission.submitted_at >= since,
            IndexingSubmission.days_to_index.is_not(None),
        )
        if client_id is not None:
            stmt = stmt.where(IndexingSubmission.client_id == client_id)
        return [int(v) for v in self._session.scalars(stmt).all() if v is not None]

    def control_group_samples(self, *, since: datetime, client_ids: list[str]) -> list[int]:
        """Сторінки, що зайшли в індекс БЕЗ жодної нашої відправки.

        Лаг рахується від `first_seen_at` (коли URL з'явився у нас) до
        `first_indexed_seen_at`. Це не ідеальний нуль-момент, але єдиний,
        який у нас справді є, і він однаковий для обох груп.
        """
        if not client_ids:
            return []
        submitted = select(IndexingSubmission.indexing_url_id).where(
            IndexingSubmission.status == "sent"
        )
        rows = self._session.execute(
            select(IndexingUrl.first_seen_at, IndexingUrl.first_indexed_seen_at).where(
                IndexingUrl.client_id.in_(client_ids),
                IndexingUrl.first_indexed_seen_at.is_not(None),
                IndexingUrl.first_indexed_seen_at >= since,
                IndexingUrl.id.not_in(submitted),
            )
        ).all()
        return [max((seen - first).days, 0) for first, seen in rows if first and seen]

    def funnel(self, *, since: datetime, client_ids: list[str] | None = None) -> dict[str, int]:
        stmt = select(
            func.count(IndexingSubmission.id),
            func.count(IndexingSubmission.id).filter(IndexingSubmission.status == "sent"),
            func.count(IndexingSubmission.id).filter(
                IndexingSubmission.status == "quota_blocked"
            ),
            func.count(IndexingSubmission.id).filter(
                IndexingSubmission.indexed_at.is_not(None)
            ),
            func.count(IndexingSubmission.id).filter(
                IndexingSubmission.status == "sent", IndexingSubmission.indexed_at.is_(None)
            ),
        ).where(IndexingSubmission.created_at >= since)
        if client_ids is not None:
            stmt = stmt.where(IndexingSubmission.client_id.in_(client_ids))
        row = self._session.execute(stmt).one()
        return {
            "planned": row[0],
            "sent": row[1],
            "quota_blocked": row[2],
            "indexed": row[3],
            "in_flight": row[4],
        }

    def reason_stats(self, *, since: datetime) -> list[tuple[str, int, int, int, list[int]]]:
        rows = self._session.execute(
            select(
                IndexingSubmission.reason_code,
                IndexingSubmission.status,
                IndexingSubmission.indexed_at,
                IndexingSubmission.days_to_index,
            ).where(IndexingSubmission.created_at >= since)
        ).all()
        buckets: dict[str, dict[str, Any]] = {}
        for reason, status, indexed_at, days in rows:
            bucket = buckets.setdefault(
                reason, {"sent": 0, "indexed": 0, "in_flight": 0, "days": []}
            )
            if status != "sent":
                continue
            bucket["sent"] += 1
            if indexed_at is not None:
                bucket["indexed"] += 1
                if days is not None:
                    bucket["days"].append(int(days))
            else:
                bucket["in_flight"] += 1
        return [
            (reason, data["sent"], data["indexed"], data["in_flight"], data["days"])
            for reason, data in sorted(buckets.items())
        ]

    def retention(self, *, horizon_days: int, client_ids: list[str]) -> tuple[int, int, int]:
        """Скільки колись проіндексованих усе ще в індексі через N днів."""
        if not client_ids:
            return 0, 0, 0
        cutoff = utcnow() - timedelta(days=horizon_days)
        row = self._session.execute(
            select(
                func.count(IndexingUrl.id),
                func.count(IndexingUrl.id).filter(IndexingUrl.is_indexed.is_(True)),
                func.count(IndexingUrl.id).filter(IndexingUrl.left_index_at.is_not(None)),
            ).where(
                IndexingUrl.client_id.in_(client_ids),
                IndexingUrl.first_indexed_seen_at.is_not(None),
                IndexingUrl.first_indexed_seen_at <= cutoff,
            )
        ).one()
        return int(row[0]), int(row[1]), int(row[2])


def median(values: list[int]) -> Decimal | None:
    """`None` на порожній вибірці — не `0`.

    Нуль тут читався б як «заходить того ж дня», тобто найкращий можливий
    результат, тоді як насправді це «нема жодного спостереження».
    """
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return Decimal(ordered[mid])
    return (Decimal(ordered[mid - 1]) + Decimal(ordered[mid])) / Decimal(2)


def percentile(values: list[int], fraction: float) -> Decimal | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(int(round(fraction * (len(ordered) - 1))), len(ordered) - 1)
    return Decimal(ordered[index])
