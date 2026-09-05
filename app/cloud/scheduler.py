"""CloudScheduler (task-52 §3.8): a thread inside the single worker process,
same shape as `MonitorScheduler`/`ReportScheduler` — a second process would
be a second SQLite writer, which PLAN §5.1 forbids.

Every tick (60s): sweep one batch of monitor blobs and one of report files.
Once a day, at or after `cloud_backup_at_utc`, also run the DB backup + GFS
promotion + prune. "Once a day" is enforced by the ledger itself
(`run_daily_backup` returns `None` if today's daily entry already exists),
not by this class tracking the last time it ran — every tick past the
configured time re-checks and finds nothing to do, so a worker restart or a
skipped tick can never produce zero OR two backups for the same day.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.cloud.backup import prune_backups, run_daily_backup
from app.cloud.client import CloudStore
from app.cloud.sweeper import prune_monitor_bodies, sweep_monitor_blobs, sweep_report_files
from app.core.config import Settings
from app.db.repositories.cloud import CloudLedgerRepository

logger = logging.getLogger(__name__)

TICK_INTERVAL_SECONDS = 60.0


def _parse_hh_mm(value: str) -> tuple[int, int]:
    hour_str, _, minute_str = value.partition(":")
    return int(hour_str), int(minute_str)


class CloudScheduler:
    def __init__(
        self,
        *,
        session_scope: Callable[[], AbstractContextManager[Session]],
        settings: Settings,
        tick_interval_seconds: float = TICK_INTERVAL_SECONDS,
        store: CloudStore | None = None,
    ) -> None:
        self._session_scope = session_scope
        self._settings = settings
        self._tick_interval_seconds = tick_interval_seconds
        self._store = store if store is not None else CloudStore(settings)
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._backup_hour, self._backup_minute = _parse_hh_mm(settings.cloud_backup_at_utc)

    def start(self) -> None:
        if not self._settings.cloud_enabled:
            logger.info("cloud_scheduler_disabled")
            return
        self._thread = threading.Thread(target=self._loop, name="cloud-scheduler", daemon=True)
        self._thread.start()
        logger.info("cloud_scheduler_started backup_at=%s", self._settings.cloud_backup_at_utc)

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=30)

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._tick()
            except Exception:  # noqa: BLE001 - one bad tick must never kill the scheduler thread
                logger.exception("cloud_scheduler_tick_failed")
            self._stop_event.wait(self._tick_interval_seconds)

    def _tick(self) -> None:
        self._run_backup_if_due()
        with self._session_scope() as session:
            sweep_monitor_blobs(session, self._store, self._settings)
            sweep_report_files(session, self._store, self._settings)
        self._run_local_retention_if_due()

    def _run_backup_if_due(self) -> None:
        now = datetime.now(UTC)
        if (now.hour, now.minute) < (self._backup_hour, self._backup_minute):
            return
        with self._session_scope() as session:
            outcome = run_daily_backup(session, self._store, self._settings)
            if outcome is None:
                return
            logger.info(
                "cloud_backup_done size_bytes=%d promoted=%s",
                outcome.size_bytes,
                outcome.promoted_tiers,
            )
            prune_backups(session, self._store, self._settings)

    def _run_local_retention_if_due(self) -> None:
        """Once a day, at or after `cloud_backup_at_utc` (same window as the
        DB backup — by then the day's monitor blobs have had many sweep
        ticks to reach B2), drop local page/resource bodies older than
        `monitor_body_retention_days`. Own idempotency key
        (`monitor_prune_day`), independent of the DB backup's: this should
        still run on a day the backup itself is a no-op (e.g. cloud storage
        briefly down and recovered), and must never run twice for one day.
        `prune_monitor_bodies` is cloud-gated by construction — nothing
        local is dropped until the ledger confirms B2 has that exact blob.
        """
        if self._settings.monitor_body_retention_days <= 0:
            return
        # Never run unattended without the cloud safety net. `cloud_enabled=False`
        # already stops this scheduler from starting at all, but
        # `cloud_sync_monitor_blobs=False` would leave the thread running while
        # `prune_monitor_bodies` falls back to age-only deletion — dropping the
        # only copy of a body nothing ever archived. The CLI's explicit
        # `--ignore-cloud` is the one supported way to prune without the net.
        if not self._settings.cloud_sync_monitor_blobs:
            return
        now = datetime.now(UTC)
        if (now.hour, now.minute) < (self._backup_hour, self._backup_minute):
            return
        date_key = now.date().isoformat()
        with self._session_scope() as session:
            ledger = CloudLedgerRepository(session)
            if ledger.is_uploaded("monitor_prune_day", date_key):
                return
            result = prune_monitor_bodies(
                session,
                self._settings,
                older_than_days=self._settings.monitor_body_retention_days,
                store=self._store,
            )
            ledger.record_uploaded(
                kind="monitor_prune_day", source_ref=date_key, key="", sha256=None, size_bytes=None
            )
            session.commit()
            logger.info(
                "monitor_local_retention_done candidates=%d removed=%d deferred_not_uploaded=%d",
                result.candidates,
                result.removed,
                result.deferred_not_uploaded,
            )
