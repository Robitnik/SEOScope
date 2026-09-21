"""Крок 2 конвеєра: URL Inspection API -> стан індексації (task-100 §2.3).

Заміряно живо (план §1.4), і це визначає весь крок: одиничний виклик коштує
6.8 с, але API **не серіалізує** запити — повний скан 715 URL у 8 потоках
дав 0.83 с/URL, тобто паралелізм лінійний і тримається на великих обсягах.
Весь портфель (2311 URL) сканується за ~33 хв.

Квота Google — 2000/добу і 600/хв **на property**; найбільший клієнт має
715 URL, тож навіть щоденний повний скан влазить із запасом ×2.8. Попри це
режим за замовчуванням — `rotating`: щодня інспектуються ті, від кого
залежать рішення (свіжовідправлені й кандидати), а решта портфеля —
ротацією раз на 7 днів. Це не економія заради економії: кожен виклик — це
6.8 с чужого API, і витрачати їх на 2001 сторінку, яка стабільно в індексі,
немає причини.

`GoogleApiSession` уже thread-safe і має backoff на 429/403, тому окремого
throttle тут немає — є лише кап на кількість потоків і на денну квоту.
"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from app.db.base import utcnow
from app.db.models import IndexingUrl
from app.db.repositories.indexing import IndexingRepository
from app.integrations.google_api.auth import GoogleApiSession
from app.urlinspector.google.index import UrlInspection, UrlInspectionClient

logger = logging.getLogger(__name__)

# Свіжовідправлені інспектуються щодня: від точності `indexed_at` залежить
# уся аналітика лагу.
FRESH_SUBMISSION_WINDOW = timedelta(days=30)
# Решта портфеля — кожен URL хоча б раз на стільки днів.
ROTATION_DAYS = 7

# `coverage_state`, що означають «Google свідомо не індексує» — ці рядки
# приходять живими (план §1.6). Матчимо нормалізованим підрядком, бо в
# `Excluded by ‘noindex’ tag` лапки ТИПОГРАФСЬКІ (U+2018/U+2019), і
# порівняння з ASCII-апострофом не спрацювало б.
_INDEXED_MARKERS = ("submitted and indexed", "indexed, not submitted in sitemap")


@dataclass
class InspectResult:
    inspected: int = 0
    failed: int = 0
    quota_exhausted: bool = False
    errors: list[str] = field(default_factory=list)


def _normalize(text: str | None) -> str:
    return (text or "").strip().lower()


def is_indexed_state(coverage_state: str | None, verdict: str | None) -> bool | None:
    """`None`, коли Google не сказав нічого визначеного.

    Порожній `coverage_state` — це не «не в індексі», це відсутність
    відповіді, і записати сюди `False` означало б вигадати кандидата
    (PLAN §12).
    """
    state = _normalize(coverage_state)
    if not state:
        return None
    if any(marker in state for marker in _INDEXED_MARKERS):
        return True
    if _normalize(verdict) == "pass" and "not indexed" not in state:
        return True
    return False


def inspection_fields(inspection: UrlInspection) -> dict[str, Any]:
    return {
        "coverage_state": inspection.coverage_state,
        "verdict": inspection.verdict,
        "robots_txt_state": inspection.robots_txt_state,
        "indexing_state": inspection.indexing_state,
        "page_fetch_state": inspection.page_fetch_state,
        "google_canonical": inspection.google_canonical,
        "user_canonical": inspection.user_canonical,
        "last_crawl_time": inspection.last_crawl_time,
        "inspection_result_link": inspection.inspection_result_link,
    }


def select_for_inspection(
    repo: IndexingRepository,
    *,
    client_id: str,
    mode: str,
    now: datetime | None = None,
) -> list[IndexingUrl]:
    """Три пріоритетні групи (план §2.3, крок 2), без дублікатів."""
    moment = now or utcnow()
    rows = [row for row in repo.urls_for_client(client_id) if row.gone_from_sitemap_at is None]
    if mode == "full":
        return rows

    fresh_ids = repo.recently_submitted_url_ids(
        client_id, since=moment - FRESH_SUBMISSION_WINDOW
    )
    rotation_cutoff = moment - timedelta(days=ROTATION_DAYS)

    picked: list[IndexingUrl] = []
    seen: set[str] = set()

    def take(row: IndexingUrl) -> None:
        if row.id not in seen:
            seen.add(row.id)
            picked.append(row)

    for row in rows:
        if row.id in fresh_ids:
            take(row)
    for row in rows:
        # Кандидат або ще не перевірений: рішення про нього ухвалюється
        # сьогодні, тож і дані мають бути сьогоднішні.
        if not row.excluded and row.is_indexed is not True:
            take(row)
    for row in rows:
        if row.inspected_at is None or row.inspected_at < rotation_cutoff:
            take(row)
    return picked


def inspect_urls(
    repo: IndexingRepository,
    session: GoogleApiSession,
    *,
    site_url: str,
    rows: list[IndexingUrl],
    workers: int = 8,
    quota_budget: int | None = None,
    heartbeat: "callable[[], None] | None" = None,
) -> InspectResult:
    """Проінспектувати перелік URL і записати зрізи.

    Мережа йде в потоках, запис у сесію — у викличному потоці: `Session`
    не thread-safe, і розкидати її по воркерах означало б міняти один
    виміряний виграш на клас помилок, які ловляться лише в проді.
    """
    result = InspectResult()
    if not rows:
        return result

    budget = len(rows) if quota_budget is None else min(len(rows), max(quota_budget, 0))
    if budget < len(rows):
        result.quota_exhausted = True
    targets = rows[:budget]
    if not targets:
        return result

    client = UrlInspectionClient(session, site_url=site_url)
    lock = threading.Lock()

    def probe(row: IndexingUrl) -> tuple[str, UrlInspection | None, str | None]:
        try:
            return row.id, client.inspect(row.url), None
        except Exception as error:  # noqa: BLE001 — падіння одного URL не валить скан
            with lock:
                logger.warning("indexing_inspect_failed url=%s err=%s", row.url, error)
            return row.id, None, type(error).__name__

    by_id = {row.id: row for row in targets}
    with ThreadPoolExecutor(max_workers=max(workers, 1)) as pool:
        for url_id, inspection, error in pool.map(probe, targets):
            row = by_id[url_id]
            if inspection is None:
                result.failed += 1
                if error and error not in result.errors:
                    result.errors.append(error)
                continue
            indexed = is_indexed_state(inspection.coverage_state, inspection.verdict)
            was_indexed = row.is_indexed
            repo.record_inspection(
                row, fields=inspection_fields(inspection), is_indexed=indexed
            )
            if indexed is True and was_indexed is not True:
                # Саме тут закривається спостереження «послали -> зайшло»:
                # перехід видно лише в момент порівняння станів.
                repo.close_indexed_submissions(row, indexed_at=row.inspected_at or utcnow())
            result.inspected += 1
            if heartbeat is not None and result.inspected % 50 == 0:
                heartbeat()
    return result
