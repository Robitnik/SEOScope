"""`DbMaintenanceScheduler` (task-68 §1): a thread inside the single worker
process, same shape as `MonitorScheduler`/`ReportScheduler`/`CloudScheduler`
— a second process would be a second SQLite writer, which PLAN §5.1
forbids.

Runs `app.db.maintenance.run_maintenance` once a day, at or after
`db_maintenance_at_utc`. "Once a day" is enforced by an in-memory last-run
date, not a durable table: unlike `CloudScheduler`'s backup ledger, a
missed or doubled run here costs nothing worse than one extra ~500ms
`ANALYZE` — a worker restart at worst re-runs it once more the same day,
which is exactly as harmless as running it twice manually. Not worth a
migration (task-68 explicitly has none) for that.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import UTC, date, datetime

from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.maintenance import run_maintenance

logger = logging.getLogger(__name__)

TICK_INTERVAL_SECONDS = 300.0


def _parse_hh_mm(value: str) -> tuple[int, int]:
    hour_str, _, minute_str = value.partition(":")
    return int(hour_str), int(minute_str)


class DbMaintenanceScheduler:
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
        self._run_hour, self._run_minute = _parse_hh_mm(settings.db_maintenance_at_utc)
        self._last_run_date: date | None = None
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._loop, name="db-maintenance-scheduler", daemon=True
        )
        self._thread.start()
        logger.info("db_maintenance_scheduler_started at=%s", self._settings.db_maintenance_at_utc)

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=30)

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._tick()
            except Exception:  # noqa: BLE001 - one bad tick must never kill the scheduler thread
                logger.exception("db_maintenance_scheduler_tick_failed")
            self._stop_event.wait(self._tick_interval_seconds)

    def _tick(self) -> None:
        now = datetime.now(UTC)
        if now.date() == self._last_run_date:
            return
        if (now.hour, now.minute) < (self._run_hour, self._run_minute):
            return
        with self._session_scope() as session:
            result = run_maintenance(session)
        self._last_run_date = now.date()
        logger.info(
            "db_maintenance_done checkpoint_busy=%d checkpoint_pages=%d",
            result.checkpoint_busy,
            result.checkpoint_pages,
        )
