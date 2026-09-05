"""Bounded retry for a transient SQLite write-lock (task-59, live-measured
2026-09-04): a worker restart's bootstrap tick fires every scheduler's
overdue work across the whole portfolio at once (Site Monitor's own 16
threads, the archive's own client pool, cloud sync, reports), and that
burst of concurrent SQLite writers can exceed `SQLITE_BUSY_TIMEOUT_MS`.

Shared between `app.webarchive.capture` (one cell's writes) and
`app.webarchive.cloud_sync` (one blob's upload bookkeeping) — both call
patterns are the same shape: do some work against the ORM (uncommitted),
then commit; on `database is locked`, roll back and redo the WHOLE thing
from scratch. That is safe specifically because a lock error means the
transaction never committed — the rollback already undid any flush — so
re-running the callable (including a re-fetch or a re-upload) produces the
same correct result, never a duplicate.
"""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable
from typing import TypeVar

from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

_T = TypeVar("_T")
# A lock error only surfaces AFTER SQLite's own `busy_timeout` (30s,
# `app.db.session`) has already been waited out — so by the time we retry,
# the writer ahead of us has been holding the lock for half a minute and
# sub-second backoff just burns the remaining attempts on the same storm.
# Live-measured (task-59, 2026-09-04): with (0.1, 0.5, 1.5) six captures
# bootstrapping at once exhausted all three attempts inside ~92s and
# crashed; the run that survived took its lock on the second attempt after
# a multi-second wait.
LOCK_RETRY_DELAYS_SECONDS = (1.0, 5.0, 15.0, 30.0)


def commit_with_retry(
    session: Session, fn: Callable[[], _T], *, delays: tuple[float, ...] | None = None
) -> _T:
    """Run `fn()` then commit `session`, retrying the whole thing a few
    times on `database is locked`. Any other `OperationalError`, or any
    other exception `fn()` raises, propagates immediately — this is a
    narrow fix for one specific, observed failure mode, not a general
    retry-everything wrapper.

    `delays` defaults to the long, "a genuinely external process is holding
    this" schedule above — resolved INSIDE the function body, not as a
    parameter default (a default is bound once, at import time, so it would
    never see a test's `monkeypatch.setattr(db_retry, "LOCK_RETRY_DELAYS_SECONDS", ...)`
    on a later call). `ArchiveWriteController` passes its own, much shorter
    one (task-62 §2.2 follow-up, live-reproduced 2026-09-05): its single
    background thread processes one job at a time, so sleeping seconds here
    on a lock held by a DIRECT (non-controller) writer — e.g. `capture.py`'s
    own `commit_with_retry`-wrapped call — blocks every OTHER already-queued
    job behind it too, including, sometimes, the very commit that would
    release that lock. A short retry fails fast instead and lets the queue
    keep moving; the caller (a heartbeat or counter update, not the page
    content itself) tolerates an occasional miss.
    """
    delays = delays if delays is not None else LOCK_RETRY_DELAYS_SECONDS
    last_exc: OperationalError | None = None
    for attempt, delay in enumerate(delays):
        try:
            result = fn()
            session.commit()
            return result
        except OperationalError as exc:
            session.rollback()
            if "database is locked" not in str(exc).lower():
                raise
            last_exc = exc
            logger.warning(
                "archive_db_locked_retry attempt=%d/%d",
                attempt + 1, len(delays),
            )
            # Jittered, not the fixed `delay` (regression, live-reproduced
            # 2026-09-05): two callers that collide once tend to have started
            # within milliseconds of each other and so keep colliding —
            # every retry lands on the exact same fixed schedule and they
            # never desynchronize. ±50% spread is enough that two colliding
            # retries drift apart within one or two cycles instead of
            # lockstepping through the whole budget together.
            time.sleep(random.uniform(delay * 0.5, delay * 1.5))
    assert last_exc is not None
    raise last_exc
