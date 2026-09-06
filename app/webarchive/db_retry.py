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

task-66 §2.4: the actual `fn()` + commit now runs through
`app.db.writegate.DbWriteGate` (the one global write queue + cross-process
flock), same as `worker_session_scope`'s own commit and
`ArchiveWriteController`'s shim. After that gate removes ordinary
contention, an `OperationalError` reaching this loop at all means some
writer went around it — the retry schedule below is deliberately much
shorter than the pre-gate `(1, 5, 15, 30)` history this file used to carry:
that long schedule was tuned to outlast a THIRTY-SECOND `busy_timeout`
(`app/db/session.py`, since lowered to 5s for the same reason — see that
setting's own docstring) that a genuinely external, uncoordinated writer
might hold for. A short schedule fails fast and logs loud instead, because
a slow retry storm here just means the actual bug (a write outside the
gate) stays invisible longer.
"""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable
from typing import TypeVar

from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.db.writegate import get_write_gate

logger = logging.getLogger(__name__)

_T = TypeVar("_T")
# Short (task-66 §2.4, see module docstring): the write gate already
# serializes every gated writer, so a `database is locked` here means a
# writer bypassed it — this schedule exists to survive a brief race with
# that stray writer, not to outlast a 30-second `busy_timeout` any more.
LOCK_RETRY_DELAYS_SECONDS = (0.1, 0.5, 1.0, 2.0)


def commit_with_retry(
    session: Session, fn: Callable[[], _T], *, delays: tuple[float, ...] | None = None
) -> _T:
    """Run `fn()` then commit `session` — via `DbWriteGate` (task-66 §2.4),
    so this is serialized against every other write in the process AND
    across the api/worker process boundary — retrying the whole thing a
    few times on `database is locked`. Any other `OperationalError`, or any
    other exception `fn()` raises, propagates immediately — this is a
    narrow fix for one specific, observed failure mode, not a general
    retry-everything wrapper.

    `delays` defaults to the short schedule above — resolved INSIDE the
    function body, not as a parameter default (a default is bound once, at
    import time, so it would never see a test's
    `monkeypatch.setattr(db_retry, "LOCK_RETRY_DELAYS_SECONDS", ...)` on a
    later call).
    """
    delays = delays if delays is not None else LOCK_RETRY_DELAYS_SECONDS
    last_exc: OperationalError | None = None
    for attempt, delay in enumerate(delays):
        try:
            # `fn()` runs HERE, on the caller's own thread — only the
            # commit hops to the gate. Live-measured 2026-09-06, this is
            # the difference between a working system and a frozen one,
            # for two independent reasons:
            #
            # 1. Callers pass real work, not a staging closure:
            #    `app/webarchive/capture.py` submits an entire page
            #    capture (HTTP fetch of the page AND every subresource).
            #    Running that inside the gate holds the ONE global
            #    cross-process flock for the whole network round-trip,
            #    so every other writer in BOTH processes — and every api
            #    request on a sync session — waits on a remote server.
            #    Both `capture.py` and `cloud_sync.py` already document
            #    this invariant ("only the actual commit is serialized");
            #    routing `fn` through the gate silently broke it.
            # 2. Those same callables submit to the gate themselves, which
            #    on the gate's single thread is a self-deadlock (see
            #    `DbWriteGate.submit`'s own note on the 2026-09-06
            #    incident). The re-entrancy guard there makes that
            #    survivable; keeping `fn` off the gate thread is what
            #    makes it not happen in the first place.
            #
            # The retry contract is unchanged: on a lock error the
            # rollback below undoes the staged work and the whole `fn` is
            # redone from scratch on the next attempt.
            result = fn()
            get_write_gate().submit(session, lambda: None)
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
