"""Site Monitor scheduler (task-35 §3.6): a loop inside the worker process.

PLAN §14 explicitly allows "a scheduler loop inside the single worker" rather
than a second service — a separate process would be a second SQLite writer,
exactly what PLAN §5.1 forbids. `app/jobs/**` (the job dispatcher/lanes) is
untouched; this is a fully independent thread with its own tick and its own
bounded worker pool.
"""

from __future__ import annotations

import hashlib
import logging
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractContextManager
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.models import Client
from app.db.repositories.clients import ClientRepository
from app.db.repositories.monitor import MonitorRepository
from app.monitor.runner import FULL, LIGHT, run_client_monitor

logger = logging.getLogger(__name__)

TICK_INTERVAL_SECONDS = 60.0
# Spreads the portfolio's first-ever ticks over ~20 minutes instead of every
# client starting in the same second (task-35 §3.6).
JITTER_SPAN_MINUTES = 20


def _jitter_minutes(client_id: str) -> int:
    digest = hashlib.sha1(client_id.encode("utf-8"), usedforsecurity=False).hexdigest()
    return int(digest, 16) % JITTER_SPAN_MINUTES


def _due(
    repo: MonitorRepository,
    client_id: str,
    *,
    kind: str,
    interval_hours: float,
    now: datetime,
) -> bool:
    last = repo.latest_run(client_id, kind=kind)
    if last is None:
        return True
    jitter = timedelta(minutes=_jitter_minutes(client_id))
    return now >= last.started_at + timedelta(hours=interval_hours) + jitter


class MonitorScheduler:
    """One tick per minute; a bounded pool of `monitor_client_workers`
    threads, each driving one client's run start to finish (task-35 §3.6).
    """

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
        self._pool = ThreadPoolExecutor(
            max_workers=max(1, settings.monitor_client_workers), thread_name_prefix="monitor"
        )
        self._in_flight: set[tuple[str, str]] = set()
        self._in_flight_lock = threading.Lock()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if not self._settings.monitor_enabled:
            logger.info("monitor_scheduler_disabled")
            return
        self._recover_stale_runs()
        self._thread = threading.Thread(target=self._loop, name="monitor-scheduler", daemon=True)
        self._thread.start()
        logger.info(
            "monitor_scheduler_started workers=%d light_hours=%s full_hours=%s",
            self._settings.monitor_client_workers,
            self._settings.monitor_light_interval_hours,
            self._settings.monitor_full_interval_hours,
        )

    def stop(self) -> None:
        self._stop_event.set()
        self._pool.shutdown(wait=True, cancel_futures=False)
        if self._thread is not None:
            self._thread.join(timeout=30)

    def _recover_stale_runs(self) -> None:
        """Runs still `running` from a previous process instance are dead —
        the process that owned them no longer exists (task-35 §3.6).
        """
        with self._session_scope() as session:
            repo = MonitorRepository(session)
            now = datetime.now(UTC)
            stale_cutoff = timedelta(minutes=self._settings.monitor_run_stale_minutes)
            recovered = 0
            for run in repo.running_runs():
                if now - run.started_at >= stale_cutoff:
                    repo.finish_run(
                        run.id, status="failed_interrupted", error_code="interrupted_by_restart"
                    )
                    recovered += 1
            session.commit()
            if recovered:
                logger.warning("monitor_scheduler_recovered_stale_runs count=%d", recovered)

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._tick()
            except Exception:  # noqa: BLE001 - one bad tick must never kill the scheduler thread
                logger.exception("monitor_scheduler_tick_failed")
            self._stop_event.wait(self._tick_interval_seconds)

    def _tick(self) -> None:
        now = datetime.now(UTC)
        with self._session_scope() as session:
            clients_repo = ClientRepository(session)
            monitor_repo = MonitorRepository(session)

            # Manual "Check now" requests (task-35 §3.6, §3.8) — the API only
            # ever writes a `queued` row; this scheduler is the sole claimer,
            # same enqueue/claim split as `SyncJob`.
            for queued in monitor_repo.queued_runs():
                client = clients_repo.get(queued.client_id)
                if client is None:
                    continue
                self._submit(client, queued.kind, trigger="manual", existing_run_id=queued.id)

            # Paused clients (task-34) drop out of the schedule unless the
            # client explicitly opted back in — `enabled=False` always wins.
            collectable_ids = {c.id for c in clients_repo.list_collectable()}
            paused_opted_in_ids = {
                c.id
                for c in clients_repo.list_active()
                if c.id not in collectable_ids and c.enabled
            }
            settings_by_client = monitor_repo.all_settings_by_client()

            candidates: list[Client] = []
            for client in clients_repo.list_active():
                if client.id not in collectable_ids and client.id not in paused_opted_in_ids:
                    continue
                client_settings = settings_by_client.get(client.id)
                if client_settings is not None and not client_settings.enabled:
                    continue
                if client.id in paused_opted_in_ids:
                    if client_settings is None or not client_settings.monitor_when_paused:
                        continue
                candidates.append(client)

            for client in candidates:
                client_settings = settings_by_client.get(client.id)
                full_interval = (
                    client_settings.full_interval_hours
                    if client_settings and client_settings.full_interval_hours
                    else self._settings.monitor_full_interval_hours
                )
                light_interval = (
                    client_settings.light_interval_hours
                    if client_settings and client_settings.light_interval_hours
                    else self._settings.monitor_light_interval_hours
                )

                full_due = _due(
                    monitor_repo, client.id, kind=FULL, interval_hours=full_interval, now=now
                )
                light_due = _due(
                    monitor_repo, client.id, kind=LIGHT, interval_hours=light_interval, now=now
                )

                if full_due and not monitor_repo.has_running_run(client.id):
                    self._submit(client, FULL, trigger="scheduled")
                # A `full` run covers everything `light` would check, so skip
                # queuing `light` behind it this same tick (task-35 §3.6
                # dedup) — and never queue a second run of the same kind
                # while one is still in flight.
                elif light_due and not monitor_repo.has_running_run(client.id):
                    self._submit(client, LIGHT, trigger="scheduled")

    def _submit(
        self, client: Client, kind: str, *, trigger: str, existing_run_id: str | None = None
    ) -> None:
        key = (client.id, kind)
        with self._in_flight_lock:
            if key in self._in_flight:
                return
            self._in_flight.add(key)

        def _run() -> None:
            try:
                with self._session_scope() as session:
                    outcome = run_client_monitor(
                        session,
                        client,
                        kind=kind,
                        settings=self._settings,
                        artifacts_dir=self._settings.artifacts_dir,
                        trigger=trigger,
                        stop_event=self._stop_event,
                        existing_run_id=existing_run_id,
                    )
                    logger.info(
                        "monitor_run_finished client_id=%s kind=%s status=%s checked=%d "
                        "truncated=%s",
                        client.id,
                        kind,
                        outcome.status,
                        outcome.urls_checked,
                        outcome.truncated,
                    )
            except Exception:  # noqa: BLE001 - one client's crash must never take down the pool
                logger.exception("monitor_run_crashed client_id=%s kind=%s", client.id, kind)
            finally:
                with self._in_flight_lock:
                    self._in_flight.discard(key)

        self._pool.submit(_run)
