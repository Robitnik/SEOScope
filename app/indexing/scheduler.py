"""IndexingScheduler (task-100 §2.8): потік усередині єдиного worker'а.

Форма та сама, що в `CloudScheduler`/`ArchiveScheduler` — другий процес
означав би другого писача, що PLAN §5.1 забороняє.

Ідемпотентність доби тримається на самому леджері (`indexing_scans`), а не
на пам'яті потоку: кожен тік після призначеної години перепитує БД і
знаходить, що сьогоднішній прогін уже був. Рестарт воркера чи пропущений
тік не можуть дати ні нуль, ні два прогони за добу — той самий урок, що
й у `app/cloud/scheduler.py`.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import UTC, datetime, time

from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.repositories.indexing import IndexingRepository
from app.indexing.service import SCAN_STALE_AFTER, run_portfolio_scan

logger = logging.getLogger(__name__)

TICK_INTERVAL_SECONDS = 60.0


def _parse_hh_mm(value: str) -> tuple[int, int]:
    hour, _, minute = value.partition(":")
    return int(hour), int(minute)


class IndexingScheduler:
    def __init__(
        self,
        *,
        session_scope: Callable[[], AbstractContextManager[Session]],
        settings: Settings,
        tick_interval_seconds: float = TICK_INTERVAL_SECONDS,
    ) -> None:
        self._session_scope = session_scope
        self._settings = settings
        self._tick_interval_seconds = tick_interval_seconds
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._hour, self._minute = _parse_hh_mm(settings.indexing_scan_at_utc)

    def start(self) -> None:
        if not self._settings.indexing_enabled:
            logger.info("indexing_scheduler_disabled")
            return
        self._thread = threading.Thread(
            target=self._loop, name="indexing-scheduler", daemon=True
        )
        self._thread.start()
        logger.info("indexing_scheduler_started at_utc=%s", self._settings.indexing_scan_at_utc)

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=10)

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.tick()
            except Exception:  # noqa: BLE001 — потік не має вмирати від одного невдалого тіку
                logger.exception("indexing_scheduler_tick_failed")
            self._stop_event.wait(self._tick_interval_seconds)

    def tick(self, *, now: datetime | None = None) -> bool:
        """Повертає `True`, якщо цей тік справді запустив прогін."""
        moment = now or datetime.now(UTC)
        due_at = datetime.combine(
            moment.date(), time(self._hour, self._minute, tzinfo=UTC)
        )
        if moment < due_at:
            return False

        with self._session_scope() as session:
            repo = IndexingRepository(session)
            if repo.reclaim_stale_scans(stale_after=SCAN_STALE_AFTER):
                session.commit()
            if repo.has_successful_scan_since(due_at):
                return False
            if repo.running_portfolio_scan(stale_after=SCAN_STALE_AFTER) is not None:
                logger.info("indexing_scheduler_skip_running")
                return False

        with self._session_scope() as session:
            result = run_portfolio_scan(
                session, trigger="scheduled", settings=self._settings
            )
            logger.info(
                "indexing_scheduled_scan_done scan=%s status=%s clients=%d",
                result.scan_id,
                result.status,
                len(result.clients),
            )
        return True
