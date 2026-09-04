"""Web Archive scheduler (task-59 §1.4): a thread inside the single worker
process — a second process would be a second SQLite writer, which PLAN §5.1
forbids (same reasoning `MonitorScheduler`/`CloudScheduler`/`ReportScheduler`
already give for their own loops). Fully independent of `app.monitor.scheduler`
— own tick, own thread pool, own tables.

Idempotency lives in the DATA (`ArchiveCapture`'s unique `(client_id,
slot_key)`), not in scheduler-thread memory: `slot_key` carries the Kyiv
wall-clock slot with its UTC offset (`2026-09-04T13:30+03:00`), so a worker
restart mid-slot re-derives "has this slot already run" from the database
and neither duplicates nor silently skips it.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractContextManager
from datetime import UTC, datetime, timedelta, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy.orm import Session

from app.cloud.client import CloudStore
from app.core.config import Settings
from app.db.models import Client
from app.db.repositories.clients import ClientRepository
from app.db.repositories.web_archive import WebArchiveRepository
from app.webarchive.capture import run_client_capture
from app.webarchive.cloud_sync import sweep_archive_blobs
from app.webarchive.db_retry import commit_with_retry

logger = logging.getLogger(__name__)

TICK_INTERVAL_SECONDS = 60.0


def _parse_slot_times(raw_times: list[str]) -> list[tuple[int, int]]:
    parsed = []
    for raw in raw_times:
        hour_str, _, minute_str = raw.partition(":")
        parsed.append((int(hour_str), int(minute_str)))
    return sorted(parsed)


def most_recent_slot(now_local: datetime, slot_times: list[tuple[int, int]]) -> datetime:
    """The most recent scheduled (hour, minute) at or before `now_local`,
    walking back to yesterday's last slot if none today has occurred yet.
    `now_local` must already be a timezone-aware datetime in the target zone.
    """
    today_candidates = [
        now_local.replace(hour=hour, minute=minute, second=0, microsecond=0)
        for hour, minute in slot_times
        if (hour, minute) <= (now_local.hour, now_local.minute)
    ]
    if today_candidates:
        return max(today_candidates)
    hour, minute = slot_times[-1]
    yesterday = now_local - timedelta(days=1)
    return yesterday.replace(hour=hour, minute=minute, second=0, microsecond=0)


def slot_key_for(moment_local: datetime) -> str:
    """`2026-09-04T13:30+03:00` — includes the UTC offset so a DST
    transition can never collide two different real moments onto the same
    key, and never silently skip a slot because of clock ambiguity.
    """
    return moment_local.isoformat(timespec="minutes")


class ArchiveScheduler:
    """One tick per minute; a bounded pool of `archive_client_workers`
    threads, each driving one client's capture start to finish.
    """

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
        self._pool = ThreadPoolExecutor(
            max_workers=max(1, settings.archive_client_workers), thread_name_prefix="archive"
        )
        self._in_flight: set[str] = set()
        self._in_flight_lock = threading.Lock()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if not self._settings.archive_enabled:
            logger.info("archive_scheduler_disabled")
            return
        self._reclaim_dead_captures()
        self._thread = threading.Thread(target=self._loop, name="archive-scheduler", daemon=True)
        self._thread.start()
        logger.info(
            "archive_scheduler_started workers=%d times=%s tz=%s",
            self._settings.archive_client_workers,
            self._settings.archive_capture_times,
            self._settings.archive_timezone,
        )

    def stop(self) -> None:
        self._stop_event.set()
        self._pool.shutdown(wait=True, cancel_futures=False)
        if self._thread is not None:
            self._thread.join(timeout=30)

    def _reclaim_dead_captures(self) -> None:
        """A row still `running` that THIS process is not actually running
        is dead, and reclaiming it is urgent: `_tick` skips any client with
        `has_running_capture`, so one dead row locks that client out of
        every later slot, silently, forever.

        Membership in `_in_flight` — not a wall-clock deadline — is what
        decides. A deadline cannot tell a legitimately long capture (SAPHNY:
        528 URLs, 52 minutes, live-measured) from a dead one; the in-flight
        set can, exactly. Two ways a row goes dead:
          * the previous process owned it (`_in_flight` is empty at boot);
          * `_run` crashed AND its except-handler could not write the
            failure either — live incident (task-59, 2026-09-04): a
            `database is locked` burst crashed six captures at once and
            then blocked the very `finish_capture` meant to record it, so
            seven clients sat at "in progress…" for three hours.
        Runs on every tick, so recovery is a minute, not the next restart.

        Known edge: `yovth-report archive-capture` runs a capture in its OWN
        process, whose rows this scheduler cannot see in `_in_flight` and so
        marks failed within a minute (the CLI overwrites the status again
        when it finishes, but for that minute the client also looks free to
        schedule). Only reachable by running the CLI against a live worker,
        which is an ops action, not the production path. The real fix is an
        owner/heartbeat column on `archive_captures` — a migration, so
        task-03's call.
        """
        with self._in_flight_lock:
            in_flight = set(self._in_flight)
        now = datetime.now(UTC)
        deadline = timedelta(minutes=self._settings.archive_capture_deadline_minutes)
        with self._session_scope() as session:
            repo = WebArchiveRepository(session)
            reclaimed = 0
            for capture in repo.running_captures():
                if capture.id in in_flight:
                    # Ours and alive. It can still be wedged (a hung socket,
                    # a throttle that never releases) — nothing here can kill
                    # its thread, but an operator should see it.
                    if capture.started_at is not None and now - capture.started_at >= deadline:
                        logger.warning(
                            "archive_capture_over_deadline capture_id=%s started_at=%s",
                            capture.id, capture.started_at,
                        )
                    continue
                repo.finish_capture(capture.id, status="failed", error_code="interrupted")
                reclaimed += 1
            if reclaimed:
                commit_with_retry(session, lambda: None)
                logger.warning("archive_scheduler_reclaimed_dead_captures count=%d", reclaimed)

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._tick()
            except Exception:  # noqa: BLE001 - one bad tick must never kill the scheduler thread
                logger.exception("archive_scheduler_tick_failed")
            self._stop_event.wait(self._tick_interval_seconds)

    def _local_now(self) -> datetime:
        tz: tzinfo
        try:
            tz = ZoneInfo(self._settings.archive_timezone)
        except ZoneInfoNotFoundError:
            logger.error(
                "archive_timezone_invalid tz=%s falling_back_to_utc",
                self._settings.archive_timezone,
            )
            tz = UTC
        return datetime.now(UTC).astimezone(tz)

    def _tick(self) -> None:
        # First, before anything decides whether a client is busy.
        self._reclaim_dead_captures()
        now_local = self._local_now()
        slot_times = _parse_slot_times(self._settings.archive_capture_times)
        target_local = most_recent_slot(now_local, slot_times)
        slot_key = slot_key_for(target_local)
        scheduled_for_utc = target_local.astimezone(UTC)

        with self._session_scope() as session:
            clients_repo = ClientRepository(session)
            repo = WebArchiveRepository(session)

            # Manual "Capture now" requests (task-59 §6) — the API only ever
            # writes a `queued` row with its own `manual:<uuid>` slot_key;
            # this scheduler is the sole claimer, same enqueue/claim split
            # Site Monitor's own scheduler uses for "Check now".
            for queued in repo.queued_captures():
                client = clients_repo.get(queued.client_id)
                if client is not None:
                    self._submit(client, queued.id)

            collectable_ids = {client.id for client in clients_repo.list_collectable()}
            candidates: list[Client] = list(clients_repo.list_collectable())
            if self._settings.archive_include_paused_clients:
                candidates += [
                    client for client in clients_repo.list_active()
                    if client.id not in collectable_ids
                ]

            for client in candidates:
                archive_settings = repo.get_or_create_settings(client.id)
                if not archive_settings.enabled:
                    continue
                if repo.get_capture_by_slot(client.id, slot_key) is not None:
                    continue
                if repo.has_running_capture(client.id):
                    continue
                capture = repo.create_capture(
                    client.id,
                    slot_key=slot_key,
                    scheduled_for=scheduled_for_utc,
                    trigger="scheduled",
                )
                session.commit()
                self._submit(client, capture.id)

            # Cloud upload (task-59 §1.8): one batch per tick, same thread —
            # not a separate scheduler, mirroring how `CloudScheduler` folds
            # its own sweep into one loop rather than spinning up another.
            if self._store.enabled:
                sweep_archive_blobs(session, self._store, self._settings)

    def _submit(self, client: Client, capture_id: str) -> None:
        with self._in_flight_lock:
            if capture_id in self._in_flight:
                return
            self._in_flight.add(capture_id)

        def _run() -> None:
            try:
                with self._session_scope() as session:
                    repo = WebArchiveRepository(session)
                    # Both bookkeeping writes go through `commit_with_retry`:
                    # they are short, but they land in the same lock storm as
                    # six other captures bootstrapping at once, and losing
                    # either one is what turns a finished capture into a row
                    # the UI shows as "in progress…" (live, 2026-09-04 —
                    # LAXTON died on exactly this `UPDATE`).
                    claimed = commit_with_retry(session, lambda: repo.claim_capture(capture_id))
                    if claimed is None:
                        return
                    outcome = run_client_capture(
                        session, client, capture_id=capture_id, settings=self._settings,
                        artifacts_dir=self._settings.artifacts_dir,
                    )
                    commit_with_retry(
                        session,
                        lambda: repo.finish_capture(capture_id, status=outcome.status),  # type: ignore[arg-type]
                    )
                    logger.info(
                        "archive_capture_finished client_id=%s capture_id=%s status=%s "
                        "new=%d unchanged=%d failed=%d blocked=%d",
                        client.id, capture_id, outcome.status,
                        outcome.urls_new, outcome.urls_unchanged, outcome.urls_failed,
                        outcome.urls_blocked,
                    )
            except Exception as exc:  # noqa: BLE001 - one client's crash must never take down the pool
                logger.exception(
                    "archive_capture_crashed client_id=%s capture_id=%s", client.id, capture_id
                )
                # Bound here, not inside the lambda: `exc` is unbound once the
                # except block ends, and this callable may be retried.
                crash_message = str(exc)[:1000]
                try:
                    with self._session_scope() as session:
                        crash_repo = WebArchiveRepository(session)
                        commit_with_retry(
                            session,
                            lambda: crash_repo.finish_capture(
                                capture_id, status="failed", error_code="crashed",
                                error_message=crash_message,
                            ),
                        )
                except Exception:  # noqa: BLE001 - best-effort bookkeeping only
                    # Not the end of the story any more: `_reclaim_dead_captures`
                    # picks the row up on the next tick, because by then this
                    # capture is out of `_in_flight`.
                    logger.exception(
                        "archive_capture_finish_after_crash_failed capture_id=%s", capture_id
                    )
            finally:
                with self._in_flight_lock:
                    self._in_flight.discard(capture_id)

        self._pool.submit(_run)
