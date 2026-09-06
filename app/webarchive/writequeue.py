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

`ArchiveWriteController` was originally its OWN single background thread
plus queue, covering only in-process contention within whichever process
constructed it — its own docstring was explicit that a second OS process
(the CLI's `archive-capture` command, or later, the api process) racing the
same SQLite file was NOT covered, falling back to `commit_with_retry`'s own
backoff.

task-66 §2.2 generalizes exactly that in-process pattern (one queue, one
thread, `Future`-based blocking) into `app.db.writegate.DbWriteGate` — a
single PROCESS-WIDE instance shared by every write in the process, not just
this module's own callers — and adds the missing cross-process half (an
OS-level `flock` around the actual commit). This class is now a thin,
backward-compatible shim delegating to that shared gate: same two
arguments, same return value, same exceptions, same blocking-until-applied
behavior, so none of this module's ~20 existing call sites
(`app.webarchive.capture`, `app.webarchive.scheduler`,
`app.webarchive.cloud_sync`) needed to change. Kept as its own class (rather
than deleting it and having callers import `get_write_gate()` directly) so
those call sites, and this module's own tests, are untouched by the
consolidation.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar

from sqlalchemy.orm import Session

from app.db.writegate import get_write_gate

_T = TypeVar("_T")


class ArchiveWriteController:
    """One instance per caller (a scheduler, a CLI command, a test fixture)
    — but every instance now delegates to the ONE process-wide
    `DbWriteGate`, so two instances never actually run writes concurrently
    even if both are "started" at once. `start()`/`stop()` are kept as
    real, idempotent no-ops-that-proxy rather than removed outright: every
    existing caller already brackets its own lifetime with them (a
    scheduler's own start-up/shutdown, a test fixture's own setup/teardown),
    and the shared gate's `submit()` self-starts lazily regardless, so
    calling `stop()` here merely drains the gate's queue up to that point
    — a LATER `submit()` (this instance's or another's) simply restarts it.
    """

    def start(self) -> None:
        get_write_gate().start()

    def stop(self) -> None:
        get_write_gate().stop()

    def submit(self, session: Session, fn: Callable[[], _T]) -> _T:
        """Blocks the calling thread until the write has been applied (or
        raised) — it does not block any OTHER thread's fetch/network work,
        only the actual commit is serialized.
        """
        return get_write_gate().submit(session, fn)
