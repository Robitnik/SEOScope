"""Worker process entrypoint (PLAN §5.1): `python -m app.jobs.worker`.

Exactly one instance runs at a time (compose's `worker` service) — this is
the single writer PLAN §5.1/§14 requires; do not scale this service beyond
one replica against a SQLite backend. Startup order is recovery first, then
the dispatcher, so no lane starts claiming cells while an interrupted lease
from a previous instance might still look `running`.
"""

from __future__ import annotations

import os
import signal
import socket
import types

from app.cloud.scheduler import CloudScheduler
from app.core.config import Settings, get_settings
from app.core.logging import configure_logging, get_logger
from app.db.maintenance_scheduler import DbMaintenanceScheduler
from app.db.session import worker_session_scope
from app.integrations.browser_session import check_session
from app.jobs.dispatcher import Dispatcher
from app.jobs.recovery import run_startup_recovery
from app.monitor.scheduler import MonitorScheduler
from app.reports.scheduler import ReportScheduler
from app.schemas.enums import Provider
from app.webarchive.scheduler import ArchiveScheduler

logger = get_logger(__name__)


def _lease_owner() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


def _parallelism(settings: Settings) -> dict[Provider, int]:
    """How many lanes each provider gets (task-31).

    Clamped to `[1, max_browser_parallel_slots]` here rather than trusted from
    the environment: this number decides how many sessions hit a provider at
    once, and a stray `BROWSER_PARALLEL_SLOTS=50` should be a slow config, not
    an account-level incident. Crawl-style providers stay single-lane — they
    already scale their own request concurrency, and the UI promises "one site
    at a time".
    """
    ceiling = settings.max_browser_parallel_slots
    default_slots = max(1, min(settings.browser_parallel_slots, ceiling))
    single = set(settings.single_lane_providers)
    overrides = settings.provider_parallel_slots

    def _for(provider: Provider) -> int:
        if provider.value in single:
            return 1
        return max(1, min(overrides.get(provider.value, default_slots), ceiling))

    return {provider: _for(provider) for provider in Provider}


def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)

    logger.info("worker_starting lease_owner=%s", _lease_owner())
    run_startup_recovery(worker_session_scope)

    parallelism = _parallelism(settings)
    logger.info("worker_parallelism %s", {p.value: n for p, n in parallelism.items()})

    dispatcher = Dispatcher(
        session_scope=worker_session_scope,
        lease_owner=_lease_owner(),
        lease_ttl_seconds=settings.job_lease_seconds,
        # task-21: the only place the real (task-20) session-liveness probe is
        # wired in — see app.jobs.runner.assume_session_alive's docstring for
        # why the job engine's own default is a no-op instead.
        check_session=check_session,
        parallelism=parallelism,
        lane_start_stagger_seconds=settings.lane_start_stagger_seconds,
    )

    # task-35: Site Monitor's own scheduler loop, independent of the job
    # dispatcher above (PLAN §14 "scheduler loop inside the single worker" —
    # a second process would be a second SQLite writer, which PLAN §5.1
    # forbids). One point-of-contact touch to `app/jobs/worker.py` (task-05
    # zone), documented in the task-35 Completion report.
    monitor_scheduler = MonitorScheduler(session_scope=worker_session_scope, settings=settings)
    monitor_scheduler.start()

    # task-42: Reports engine's own scheduler loop, independent of both the
    # job dispatcher and MonitorScheduler above (PLAN §14 "scheduler loop
    # inside the single worker" — a second process would be a second SQLite
    # writer, which PLAN §5.1 forbids). One point-of-contact touch to
    # `app/jobs/worker.py` (task-05 zone), documented in the task-42
    # Completion report, same pattern as task-35's MonitorScheduler above.
    report_scheduler = ReportScheduler(session_scope=worker_session_scope, settings=settings)
    report_scheduler.start()

    # task-52: cloud storage's own scheduler loop, independent of the job
    # dispatcher and the two schedulers above (PLAN §14 "scheduler loop
    # inside the single worker" — a second process would be a second SQLite
    # writer, which PLAN §5.1 forbids). One point-of-contact touch to
    # `app/jobs/worker.py` (task-05 zone), documented in the task-52
    # Completion report, same pattern as MonitorScheduler/ReportScheduler.
    cloud_scheduler = CloudScheduler(session_scope=worker_session_scope, settings=settings)
    cloud_scheduler.start()

    # task-59: Web Archive's own scheduler loop, independent of the job
    # dispatcher and the three schedulers above (PLAN §14 "scheduler loop
    # inside the single worker" — a second process would be a second SQLite
    # writer, which PLAN §5.1 forbids). One point-of-contact touch to
    # `app/jobs/worker.py` (task-05 zone), documented in the task-59
    # Completion report, same pattern as MonitorScheduler/ReportScheduler/
    # CloudScheduler above.
    archive_scheduler = ArchiveScheduler(session_scope=worker_session_scope, settings=settings)
    archive_scheduler.start()

    # task-68 §1: `ANALYZE`/`wal_checkpoint(TRUNCATE)`/`PRAGMA optimize`'s
    # own scheduler loop, independent of the four schedulers above (PLAN
    # §14 "scheduler loop inside the single worker" — a second process
    # would be a second SQLite writer, which PLAN §5.1 forbids). One
    # point-of-contact touch to `app/jobs/worker.py` (task-05 zone), same
    # pattern as MonitorScheduler/ReportScheduler/CloudScheduler/
    # ArchiveScheduler above.
    db_maintenance_scheduler = DbMaintenanceScheduler(
        session_scope=worker_session_scope, settings=settings
    )
    db_maintenance_scheduler.start()

    def _handle_signal(signum: int, _frame: types.FrameType | None) -> None:
        logger.info("worker_shutdown_signal signum=%s", signum)
        dispatcher.stop()
        monitor_scheduler.stop()
        report_scheduler.stop()
        cloud_scheduler.stop()
        archive_scheduler.stop()
        db_maintenance_scheduler.stop()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    dispatcher.run_forever()
    logger.info("worker_stopped")


if __name__ == "__main__":
    main()
