"""One global SQLite write queue for the whole system (task-66 §2).

Live-measured 2026-09-05 on the production DB: the writer lock was busy
**97% of a 30s sample** during ordinary archive-capture + cloud-sync
traffic, producing 292 `database is locked` errors in the api process over
24h and outright losing cloud uploads (`archive_blob_upload_failed
error=OperationalError`). `ArchiveWriteController` (task-62) already proved
the fix for its own corner: a single background thread applying one commit
at a time removes in-process contention entirely. Its own docstring is
explicit about what it does NOT cover — a second OS process (api vs
worker) racing the same SQLite file — because nothing coordinated across
that boundary.

`DbWriteGate` generalizes the same in-process pattern (one queue, one
thread, `Future`-based blocking) to every write in the process — not just
`ArchiveWriteController`'s own callers — and adds the missing cross-process
half: an OS-level `flock` taken around the actual `session.commit()`. Two
processes cannot both hold that flock at once, so the api and worker can
never both be mid-write at the same SQLite-transaction moment, without
either process knowing anything about the other beyond a shared file path.

Deliberately NOT covering: async (`sqlite+aiosqlite`) sessions built via
`app.db.session.get_db` (used by a handful of low-volume, interactive
`clients.py` CRUD endpoints). Every observed incident (§0 of the task plan)
traced to the SYNC path (`worker_session_scope`, `commit_with_retry`,
`ArchiveWriteController`) — bridging the async path through this
thread-based gate would need its own `run_in_threadpool`-style hop for
comparatively little payoff, and is left as a follow-up if it ever shows up
in an incident.

task-75: this whole class exists to serialize SQLite's single writer lock
— a problem Postgres does not have (real MVCC concurrency control). On a
non-SQLite `DATABASE_URL`, `submit()` bypasses the queue, the background
thread, and the cross-process `flock` entirely and just runs `fn()` then
`session.commit()` inline; `start()`/`stop()` become no-ops. This is not a
degraded fallback — it is the exact same "run inline" branch `submit()`
already took for its SQLite re-entrancy case, just correct for every
caller here instead of only the nested one. The alternative (leaving the
machinery "on") would be actively broken, not merely wasteful: the idle
checkpointer's `_checkpoint()` opens a raw `sqlite3.connect(self._db_path)`
that has no sqlite file to point at once `database_url` is a Postgres URL.
"""

from __future__ import annotations

import contextlib
import fcntl
import logging
import queue
import sqlite3
import threading
import time
from collections.abc import Callable, Generator
from concurrent.futures import Future
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Generic, TypeVar

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.session import default_write_lock_path, sqlite_db_path, sqlite_sync_url

logger = logging.getLogger(__name__)

_T = TypeVar("_T")


@dataclass
class _WriteJob(Generic[_T]):
    session: Session
    fn: Callable[[], _T]
    future: Future[_T] = field(default_factory=Future)
    submitted_at: float = field(default_factory=time.monotonic)


_STOP = object()

# Cap on the idle checkpointer's back-off after a busy WAL: long enough that a
# sustained burst is not re-probed constantly, short enough that the space is
# reclaimed promptly once the burst ends.
_CHECKPOINT_BACKOFF_MAX = 60.0


@contextlib.contextmanager
def _flock(path: Path) -> Generator[None, None, None]:
    """Exclusive OS-level lock around the actual commit (task-66 §2.1).

    `fcntl.flock` is released automatically by the kernel if the holding
    process dies — unlike a lock row in the database itself, a crashed
    process can never leave this one stuck. A plain empty file, not the
    database file itself: taking a second `flock` on the SQLite file would
    fight with SQLite's own file locking (which is what got us here).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = open(path, "a+b")  # noqa: SIM115 - closed in `finally`, held for the lock's duration
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        fd.close()


class DbWriteGate:
    """One instance per process (module-level singleton via `get_write_gate`).

    Same `submit(session, fn) -> result` shape as task-62's
    `ArchiveWriteController.submit` on purpose — that class becomes a thin
    delegating shim over this gate (`app/webarchive/writequeue.py`), so
    none of its ~20 existing call sites needed to change.
    """

    def __init__(
        self,
        lock_path: Path | None = None,
        *,
        slow_ms: int | None = None,
        strict: bool | None = None,
        db_path: Path | None = None,
        checkpoint_idle_s: float | None = None,
        checkpoint_min_wal_mb: float | None = None,
    ) -> None:
        settings = get_settings()
        # task-75: every mechanism below (queue, thread, flock, idle
        # checkpointer) exists solely to serialize SQLite's single writer
        # lock. On Postgres, `submit()` skips straight to `fn()` + `commit()`
        # — see the module docstring — so `start()`/`stop()` also no-op and
        # this stays `False`-and-unused rather than pointed at a path
        # derived from a URL that was never a SQLite file to begin with.
        self._is_sqlite = sqlite_sync_url(settings.database_url).startswith("sqlite://")
        self._lock_path = lock_path if lock_path is not None else default_write_lock_path(settings)
        self._slow_ms = settings.db_write_gate_slow_ms if slow_ms is None else slow_ms
        self._strict = settings.db_write_gate_strict if strict is None else strict
        self._db_path = db_path if db_path is not None else sqlite_db_path(settings)
        self._checkpoint_idle_s = (
            settings.db_checkpoint_idle_s if checkpoint_idle_s is None else checkpoint_idle_s
        )
        self._checkpoint_min_wal_mb = (
            settings.db_checkpoint_min_wal_mb
            if checkpoint_min_wal_mb is None
            else checkpoint_min_wal_mb
        )
        self._checkpoint_backoff = 0.0
        self._checkpoint_skip_until = 0.0
        self._queue: queue.Queue[Any] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._start_lock = threading.Lock()

    def start(self) -> None:
        if not self._is_sqlite:
            return
        with self._start_lock:
            if self._thread is not None:
                return
            self._thread = threading.Thread(
                target=self._run, name="db-write-gate", daemon=True
            )
            self._thread.start()

    def stop(self) -> None:
        if not self._is_sqlite:
            return
        with self._start_lock:
            if self._thread is None:
                return
            self._queue.put(_STOP)
            self._thread.join(timeout=30)
            self._thread = None

    def submit(self, session: Session, fn: Callable[[], _T]) -> _T:
        """Blocks the CALLING thread until the write has been applied (or
        raised) — never any other thread's own work, only the actual
        commit is serialized. Self-starts on first use: unlike a
        scheduler's own long-lived loop (which calls `start()`/`stop()`
        around its lifetime), this gate is also reached from one-shot
        request handlers and CLI commands with no lifecycle hook to use.

        Non-SQLite target: no queue, no thread, no flock — see the module
        and `__init__` docstrings. Postgres's own MVCC already gives every
        caller real concurrent commits; nothing here would add safety, and
        the idle checkpointer below would actively break.
        """
        if not self._is_sqlite:
            result = fn()
            session.commit()
            return result
        self.start()
        if threading.current_thread() is self._thread:
            # RE-ENTRANT call: we are already ON the gate thread, inside
            # `_process`'s own flock. Queueing here would make this thread
            # wait for a job only it can run — a permanent self-deadlock
            # that ALSO never releases the cross-process flock, so the
            # other process's gate blocks forever too, and with it every
            # request that touches `worker_session_scope`.
            #
            # Not hypothetical, and not a rare race: it is the guaranteed
            # outcome of any gated `fn()` that itself writes.
            # LIVE INCIDENT 2026-09-06 — `commit_with_retry` submitted
            # `_capture_url_and_commit` (a whole page capture) as its
            # `fn`; that function's own closing
            # `write_controller.submit(session, lambda: None)`
            # (`app/webarchive/capture.py`) re-entered here on the very
            # first archived URL after start-up. The worker froze at
            # 05:16:49 holding the flock, and every api endpoint using a
            # sync session hung until the container was restarted.
            #
            # Running inline is not a fallback, it is the CORRECT
            # semantics: the flock is already held by this thread and the
            # gate's whole contract is "one write at a time", which a
            # nested call on this same thread already satisfies. The
            # result is exactly what the caller would have got with no
            # gate at all.
            result = fn()
            session.commit()
            return result
        job: _WriteJob[_T] = _WriteJob(session=session, fn=fn)
        self._queue.put(job)
        return job.future.result()

    def _run(self) -> None:
        while True:
            if self._checkpoint_idle_s <= 0:
                job = self._queue.get()
            else:
                try:
                    job = self._queue.get(timeout=self._checkpoint_idle_s)
                except queue.Empty:
                    # Nothing queued for a whole idle window. This thread is
                    # the process's only writer, so "queue empty" really is
                    # "nobody is waiting on a write" — the one moment when
                    # spending time on a checkpoint costs nobody anything.
                    self._checkpoint()
                    continue
            if job is _STOP:
                return
            self._process(job)

    def _checkpoint(self) -> None:
        """Reclaim the WAL, deliberately and while idle (task-67 §2).

        This is the other half of `sqlite_wal_autocheckpoint=0`. SQLite's
        own automatic checkpoint does the same work at the worst possible
        moment — inside whichever ordinary commit happens to overflow the
        WAL, holding the writer lock for a measured 334-432ms while
        everything else queues behind it. Doing it here instead does not
        make the work cheaper (measured: 455ms to reclaim an 85MB WAL);
        it makes it happen when no one is waiting.

        `TRUNCATE` (not `PASSIVE`) because only TRUNCATE actually returns
        the space: a PASSIVE checkpoint during live traffic was measured
        leaving the WAL at its full 85MB.

        **`busy_timeout=0` is load-bearing, not tidiness.** TRUNCATE waits
        for readers, and this runs while holding the cross-process flock,
        so any wait here is a wait imposed on every writer in BOTH
        processes. Live-measured 2026-09-06 with a 1000ms timeout, during
        a real portfolio capture:

            slow_job duration_ms=1012..1023 queue_wait_ms=4912..5002  (x21)

        Every one of those was a worker commit stuck behind this thread's
        blocking checkpoint — the exact stall this whole task exists to
        remove, reintroduced by its own fix. At `0` the checkpoint either
        gets its locks instantly or gives up instantly, and the flock is
        held for the duration of real work only.

        Giving up is the normal case under load, not a failure: reads are
        ~4ms, so gaps between them are frequent and a later window lands
        in one. `_checkpoint_backoff` keeps a busy WAL from being retried
        every idle window, since under sustained load the answer will not
        change for a while.

        A dedicated short-lived connection rather than the shared engine's
        pool: `busy_timeout=0` on a pooled connection would leak that
        value to whoever got the connection next — turning every future
        `SQLITE_BUSY` in an unrelated caller into an instant failure.
        """
        wal = Path(f"{self._db_path}-wal")
        try:
            wal_mb = wal.stat().st_size / 1e6
        except OSError:
            return  # no WAL yet, or the DB file moved out from under us
        if wal_mb < self._checkpoint_min_wal_mb:
            return
        if self._checkpoint_skip_until > time.monotonic():
            return
        start = time.monotonic()
        try:
            with _flock(self._lock_path):
                conn = sqlite3.connect(str(self._db_path), isolation_level=None)
                try:
                    conn.execute("PRAGMA busy_timeout=0")
                    busy, _, _ = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                finally:
                    conn.close()
        except Exception as exc:  # noqa: BLE001 - a checkpoint is best-effort by design
            logger.warning("db_write_gate_checkpoint_failed error=%s", type(exc).__name__)
            return
        duration_ms = (time.monotonic() - start) * 1000
        if busy:
            # A reader held the WAL. Nothing is wrong and nothing is lost —
            # back off so a sustained burst is not re-probed every window.
            self._checkpoint_backoff = min(
                max(self._checkpoint_backoff * 2, self._checkpoint_idle_s), _CHECKPOINT_BACKOFF_MAX
            )
            self._checkpoint_skip_until = time.monotonic() + self._checkpoint_backoff
            logger.debug(
                "db_write_gate_checkpoint_busy wal_mb=%.1f duration_ms=%.0f backoff_s=%.1f",
                wal_mb, duration_ms, self._checkpoint_backoff,
            )
            return
        self._checkpoint_backoff = 0.0
        self._checkpoint_skip_until = 0.0
        logger.info(
            "db_write_gate_checkpoint wal_mb=%.1f duration_ms=%.0f", wal_mb, duration_ms
        )

    def _process(self, job: _WriteJob[Any]) -> None:
        queue_wait_ms = (time.monotonic() - job.submitted_at) * 1000
        start = time.monotonic()
        try:
            with _flock(self._lock_path):
                result = job.fn()
                job.session.commit()
        except BaseException as exc:  # noqa: BLE001 - travels via Future, not a raise here
            # A failed commit leaves the session's own transaction state
            # dirty; `session.close()` in the CALLER's own `finally` (every
            # existing caller already has one — `worker_session_scope`,
            # `commit_with_retry`) resets the pooled connection on return,
            # so no rollback is needed on this thread.
            job.future.set_exception(exc)
            return
        duration_ms = (time.monotonic() - start) * 1000
        if duration_ms > self._slow_ms:
            message = (
                f"db_write_gate_slow_job duration_ms={duration_ms:.0f} "
                f"queue_wait_ms={queue_wait_ms:.0f} threshold_ms={self._slow_ms}"
            )
            # Logged BEFORE the future resolves either way (strict or not)
            # — `submit()` unblocks the CALLING thread the instant the
            # future is set, and that thread must never be able to observe
            # "the write is done" without this warning already having been
            # emitted (this class of ordering bug is exactly what a
            # `caplog`-based test would otherwise flake on).
            logger.warning(message)
            if self._strict:
                # The write already committed — strict mode exists to make
                # a regression (blocking I/O leaking into a gated
                # transaction) impossible to miss in CI, not to undo the
                # write itself.
                job.future.set_exception(RuntimeError(message))
                return
        job.future.set_result(result)


_gate: DbWriteGate | None = None
_gate_init_lock = threading.Lock()


def get_write_gate() -> DbWriteGate:
    global _gate
    if _gate is None:
        with _gate_init_lock:
            if _gate is None:
                _gate = DbWriteGate()
    return _gate


def reset_write_gate_for_tests() -> None:
    """Test-only: drop the singleton so a fresh `Settings` (e.g. a
    monkeypatched `db_write_lock_path`/`db_write_gate_strict`) takes effect
    on the next `get_write_gate()` call.
    """
    global _gate
    if _gate is not None:
        _gate.stop()
    _gate = None
