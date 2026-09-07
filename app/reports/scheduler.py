"""Report scheduler (task-42 §2.3), the reports engine's own loop inside the
single worker process — a second process would be a second SQLite writer,
which PLAN §5.1 forbids. Mirrors `app.monitor.scheduler.MonitorScheduler`'s
shape: a background thread ticking every `tick_interval_seconds`, a bounded
`ThreadPoolExecutor` per path, and startup recovery for orphaned `running`
rows before the loop starts.

Four independent pools, not one: standalone API-path runs
(`reports_api_workers`, default 3), batch API-path runs
(`reports_batch_client_workers`, default 3, task-47 §2.3), the direct Google
API source (`reports_google_api_workers`, task-84 §3.3 — no browser, no
Reporting Ninja rate limit, so it can run wider), and the PDF path
(hard-capped at 1 — one login, one browser, one cabinet session for the
whole process, §2.3) never compete for the same slot. Without the
batch/standalone split, a 14-client batch would occupy every API slot and an
operator's unrelated single-client report would queue behind the whole
batch (task-47 §2.3).

`source` in `claim_next_queued` is a plain string filter, so a
`source="google_api"` run needs its OWN `_fill` call — without it that run
is never claimed and sits `queued` forever with no error (task-84 §0.3).
"""

from __future__ import annotations

import logging
import os
import socket
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from app.core.config import Settings
from app.db.repositories.reports import ReportRepository
from app.reports.batch import reconcile_batches
from app.reports.runner import run as run_report

logger = logging.getLogger(__name__)

TICK_INTERVAL_SECONDS = 2.0


class ReportScheduler:
    def __init__(
        self,
        *,
        session_scope: Any,
        settings: Settings,
        tick_interval_seconds: float = TICK_INTERVAL_SECONDS,
    ) -> None:
        self._session_scope = session_scope
        self._settings = settings
        self._tick_interval_seconds = tick_interval_seconds

        self._api_capacity = max(1, settings.reports_api_workers)
        self._batch_api_capacity = max(1, settings.reports_batch_client_workers)
        self._google_api_capacity = max(1, settings.reports_google_api_workers)
        self._pdf_capacity = 1  # NOT configurable — see module docstring
        self._api_pool = ThreadPoolExecutor(
            max_workers=self._api_capacity, thread_name_prefix="reports-api"
        )
        self._batch_api_pool = ThreadPoolExecutor(
            max_workers=self._batch_api_capacity, thread_name_prefix="reports-batch-api"
        )
        self._google_api_pool = ThreadPoolExecutor(
            max_workers=self._google_api_capacity, thread_name_prefix="reports-google-api"
        )
        self._pdf_pool = ThreadPoolExecutor(
            max_workers=self._pdf_capacity, thread_name_prefix="reports-pdf"
        )
        self._in_flight_api: set[str] = set()
        self._in_flight_batch_api: set[str] = set()
        self._in_flight_google_api: set[str] = set()
        self._in_flight_pdf: set[str] = set()
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if not self._settings.reports_enabled:
            logger.info("report_scheduler_disabled")
            return
        self._recover_stale_runs()
        self._thread = threading.Thread(target=self._loop, name="report-scheduler", daemon=True)
        self._thread.start()
        logger.info(
            "report_scheduler_started api_workers=%d google_api_workers=%d pdf_workers=%d",
            self._api_capacity, self._google_api_capacity, self._pdf_capacity,
        )

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=30)
        self._api_pool.shutdown(wait=True)
        self._batch_api_pool.shutdown(wait=True)
        self._google_api_pool.shutdown(wait=True)
        self._pdf_pool.shutdown(wait=True)

    def _recover_stale_runs(self) -> None:
        with self._session_scope() as session:
            recovered = ReportRepository(session).recover_stale_running()
        if recovered:
            logger.warning("report_scheduler_recovered_stale_runs count=%d", recovered)
        # A batch whose children all finished (or were recovered above) while
        # the worker was down must not stay "running" forever — task-47 DoD
        # "Рестарт worker посеред батчу … recovery доводить батч до
        # термінального стану".
        reconcile_batches(self._session_scope)

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._tick()
            except Exception:  # noqa: BLE001 - one bad tick must not kill the scheduler
                logger.exception("report_scheduler_tick_failed")
            self._stop_event.wait(self._tick_interval_seconds)

    def _tick(self) -> None:
        self._fill(
            "api", self._in_flight_api, self._api_capacity, self._api_pool, batch_only=False
        )
        self._fill(
            "api", self._in_flight_batch_api, self._batch_api_capacity, self._batch_api_pool,
            batch_only=True,
        )
        # One pool for `google_api`, standalone and batch children alike
        # (task-84 §3.3): the direct transport has neither a browser nor
        # Reporting Ninja's 300/min account limit, so a batch cannot starve an
        # ad-hoc run the way it could on the `api` path.
        self._fill(
            "google_api", self._in_flight_google_api, self._google_api_capacity,
            self._google_api_pool,
        )
        self._fill("pdf", self._in_flight_pdf, self._pdf_capacity, self._pdf_pool)
        try:
            reconcile_batches(self._session_scope)
        except Exception:  # noqa: BLE001 - a broken reconcile must not stop the claim loop
            logger.exception("report_scheduler_batch_reconcile_failed")

    def _fill(
        self,
        source: str,
        in_flight: set[str],
        capacity: int,
        pool: ThreadPoolExecutor,
        *,
        batch_only: bool | None = None,
    ) -> None:
        """Claim and submit `queued` runs of one `source` (and, for the API
        path, one pool — standalone or batch, task-47 §2.3) until either its
        capacity is full or there is nothing left to claim.
        """
        while True:
            with self._lock:
                if len(in_flight) >= capacity:
                    return
            with self._session_scope() as session:
                claimed = ReportRepository(session).claim_next_queued(
                    source, lease_owner=self._lease_owner(), batch_only=batch_only
                )
            if claimed is None:
                return
            with self._lock:
                in_flight.add(claimed.id)
            pool.submit(self._run_and_release, claimed.id, in_flight)

    def _run_and_release(self, run_id: str, in_flight: set[str]) -> None:
        try:
            run_report(self._session_scope, self._settings, run_id)
        except Exception:  # noqa: BLE001 - a crashed run must still free its slot
            logger.exception("report_run_crashed run_id=%s", run_id)
        finally:
            with self._lock:
                in_flight.discard(run_id)

    @staticmethod
    def _lease_owner() -> str:
        return f"{socket.gethostname()}:{os.getpid()}:{threading.get_ident()}"
