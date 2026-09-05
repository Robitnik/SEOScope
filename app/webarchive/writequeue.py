"""Serializes archive DB writes across threads without limiting how many
captures fetch in parallel (task-62 §2.2).

SQLite WAL allows any number of concurrent readers but exactly one writer.
`ArchiveScheduler` runs up to `archive_client_workers` captures at once, each
in its own thread with its own `Session`, each calling `commit_with_retry`
independently — live-measured 2026-09-05: a portfolio run produced
`archive_db_locked_retry` bursts as several of those sessions tried to
commit around the same moment. The fetch itself (network I/O, already
serialized per-host by `HostThrottle`) is not the bottleneck and gains
nothing from being throttled further; the actual write is what needs to
never overlap.

`ArchiveWriteController` is a drop-in replacement for calling
`commit_with_retry(session, fn)` directly: same two arguments, same return
value, same exceptions. The difference is WHERE `fn()` and `session.commit()`
actually run — always on this controller's own single background thread,
one job at a time, so no two `commit()`s from this process are ever in
flight together. Each caller keeps its own `Session` (built by its own
`session_scope()` inside its own worker thread, exactly as today); the
controller only ever touches whichever session's job it is currently
running, handed off through the queue, and the submitting thread blocks on
its own `Future` in the meantime rather than touching that session
concurrently — so no `Session` is ever used from two threads at once.

This does NOT cover the CLI's `archive-capture` command, which runs in its
own OS process with its own controller instance (or none at all, still
calling `commit_with_retry` directly) — cross-process contention still
falls back to SQLite's own busy_timeout + `commit_with_retry`'s backoff,
which is fine: that is a rare tail, not the 6-way in-process pileup this
fixes.
"""

from __future__ import annotations

import logging
import queue
import threading
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass
from typing import Any, Generic, TypeVar

from sqlalchemy.orm import Session

from app.webarchive.db_retry import commit_with_retry

logger = logging.getLogger(__name__)

_T = TypeVar("_T")


@dataclass
class _WriteJob(Generic[_T]):
    session: Session
    fn: Callable[[], _T]
    future: Future[_T]


_STOP = object()


class ArchiveWriteController:
    """One instance per `ArchiveScheduler` (one per worker process). Start it
    before submitting anything; stop it on shutdown so its thread doesn't
    outlive the process.
    """

    def __init__(self) -> None:
        self._queue: queue.Queue[Any] = queue.Queue()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run, name="archive-write-controller", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._queue.put(_STOP)
        if self._thread is not None:
            self._thread.join(timeout=30)

    def submit(self, session: Session, fn: Callable[[], _T]) -> _T:
        """Blocks the calling thread until the write has been applied (or
        raised) — it does not block any OTHER thread's fetch/network work,
        only the actual commit is serialized.
        """
        job: _WriteJob[_T] = _WriteJob(session=session, fn=fn, future=Future())
        self._queue.put(job)
        return job.future.result()

    def _run(self) -> None:
        while True:
            job = self._queue.get()
            if job is _STOP:
                return
            try:
                job.future.set_result(commit_with_retry(job.session, job.fn))
            except BaseException as exc:  # noqa: BLE001 - travels via Future, not a raise here
                job.future.set_exception(exc)
