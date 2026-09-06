"""A capture's liveness signal, written OUTSIDE `DbWriteGate` (task-69 §1).

`heartbeat_at` is what `ArchiveScheduler._reclaim_dead_captures` trusts to
tell a capture that is alive-but-slow from one that is actually dead — but
until now every write of it went through the SAME serialized write queue as
every other commit in the process (bulk counters, asset rows, cloud-sync
blobs). That makes it the first casualty of exactly the condition it exists
to detect: heavy write contention. Live-measured 2026-09-06, with
`DbWriteGate`'s own deadlock already fixed (task-67) and zero
`database is locked` for the following hour: ALPHA's capture still lost 131
seconds of heartbeat around a burst of six clients bootstrapping their own
first commits at once, and `_reclaim_dead_captures` killed it at exactly
the 90s mark — a real, working capture, reported `failed/interrupted` with
no explanation (see the frontend/js/archive-capture.js completion report,
same date).

This module is a second, independent path for that ONE column: a raw
`UPDATE`, no ORM session, no queue — a short-lived direct connection
guarded by the SAME cross-process flock `DbWriteGate` takes
(`app.db.session.default_write_lock_path`), but taken NON-BLOCKING. If the
gate (or the idle checkpointer) already holds it, this skips instantly
rather than queuing — correct because the caller already flushes the
authoritative counters/heartbeat through the normal gated path moments
later in the same loop iteration; this is purely an extra, cheap chance for
the timestamp to land before 90 seconds pass, never the only writer of it.

Deliberately NOT added to `app/db/writegate.py` itself — that module is
task-67's owned path, and this needs none of its machinery (no
Future-based blocking, no queue, no re-entrancy guard: a heartbeat write
that cannot get the lock RIGHT NOW should skip, not wait).
"""

from __future__ import annotations

import fcntl
import logging
import sqlite3
from datetime import UTC, datetime

from app.core.config import Settings
from app.db.session import default_write_lock_path, sqlite_db_path, sqlite_sync_url

logger = logging.getLogger(__name__)

# Long enough for a single UPDATE by rowid-equivalent primary key (microseconds
# in practice) to finish even on a cold page-cache miss, short enough that a
# genuinely busy database gives up immediately rather than piling this
# connection up behind the gate's own work.
_BUSY_TIMEOUT_MS = 200


def refresh_heartbeat_best_effort(settings: Settings, capture_id: str) -> bool:
    """Best-effort `archive_captures.heartbeat_at = now()` for `capture_id`.

    Returns `True` on a successful write, `False` on ANY failure (lock held
    elsewhere, database busy, file gone, capture row deleted) — every
    failure mode is silent by design (`logger.debug`, not `warning`): the
    caller already has a normal, authoritative heartbeat write later in the
    same flush; this function losing a race is the expected, harmless case,
    not an incident.

    task-75: `False` immediately on a non-SQLite target, before touching
    any `sqlite3`-specific code. This whole module exists to work around
    SQLite's single-writer lock — the exact contention that made
    `heartbeat_at` the first casualty (module docstring above) — which
    Postgres does not have; the caller's normal, gated write (now a plain
    commit there too, `app/db/writegate.py`) already lands promptly.
    """
    if not sqlite_sync_url(settings.database_url).startswith("sqlite://"):
        return False
    lock_path = default_write_lock_path(settings)
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = open(lock_path, "a+b")  # noqa: SIM115 - closed in `finally` below
    except OSError:
        return False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            # The gate (or the idle checkpointer) holds it right now — the
            # whole point is to never wait for that.
            logger.debug("archive_heartbeat_direct_skipped capture_id=%s reason=locked", capture_id)
            return False
        try:
            return _write(settings, capture_id)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        fd.close()


def _write(settings: Settings, capture_id: str) -> bool:
    try:
        conn = sqlite3.connect(
            str(sqlite_db_path(settings)), isolation_level=None, timeout=_BUSY_TIMEOUT_MS / 1000
        )
    except sqlite3.Error:
        return False
    try:
        conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
        cursor = conn.execute(
            "UPDATE archive_captures SET heartbeat_at = ? WHERE id = ?",
            (datetime.now(UTC).isoformat(sep=" "), capture_id),
        )
        return cursor.rowcount > 0
    except sqlite3.Error as exc:
        logger.debug(
            "archive_heartbeat_direct_failed capture_id=%s error=%s", capture_id, type(exc).__name__
        )
        return False
    finally:
        conn.close()
