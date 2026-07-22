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

from app.core.config import get_settings
from app.core.logging import configure_logging, get_logger
from app.db.session import worker_session_scope
from app.integrations.browser_session import check_session
from app.jobs.dispatcher import Dispatcher
from app.jobs.recovery import run_startup_recovery

logger = get_logger(__name__)


def _lease_owner() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)

    logger.info("worker_starting lease_owner=%s", _lease_owner())
    run_startup_recovery(worker_session_scope)

    dispatcher = Dispatcher(
        session_scope=worker_session_scope,
        lease_owner=_lease_owner(),
        lease_ttl_seconds=settings.job_lease_seconds,
        # task-21: the only place the real (task-20) session-liveness probe is
        # wired in — see app.jobs.runner.assume_session_alive's docstring for
        # why the job engine's own default is a no-op instead.
        check_session=check_session,
    )

    def _handle_signal(signum: int, _frame: types.FrameType | None) -> None:
        logger.info("worker_shutdown_signal signum=%s", signum)
        dispatcher.stop()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    dispatcher.run_forever()
    logger.info("worker_stopped")


if __name__ == "__main__":
    main()
