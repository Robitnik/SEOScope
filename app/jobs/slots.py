"""Browser-lane slots (task-31).

A "slot" is one of the N parallel lanes a provider may drive on a single
logged-in profile — physically, one window of the warm host Chrome
(`seo_audit.browser.session.find_or_open_window(..., slot=)`), driven by one
CDP connection. The slot index is what keeps two concurrent cells of the SAME
provider from grabbing the same window: window identity is per
`(provider, slot)`, not per provider.

Why in-process and not a table: exactly one worker process runs (PLAN §5.1),
so the pool is authoritative for this machine, and a slot that was held by a
process that died is gone with it — a persisted pool would instead need its own
recovery pass to release phantom holders. Cross-process safety does not rest on
this at all: it rests on the DB capacity check inside the claim UPDATE
(`CellRepository.claim_next_cell`) and on the per-slot `flock` AISeoTools takes.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager


class SlotPool:
    """Hands out the lowest free lane index for a key, blocking-free.

    `acquire()` returns `None` when every lane is busy — the caller (a lane
    thread) must then not claim work, rather than queue behind a slot it cannot
    name. That mirrors the DB side: capacity full means "come back later", never
    "wait here holding nothing".
    """

    def __init__(self, capacity: int) -> None:
        if capacity < 1:
            raise ValueError(f"capacity must be >= 1, got {capacity}")
        self._capacity = capacity
        self._lock = threading.Lock()
        self._taken: set[int] = set()

    @property
    def capacity(self) -> int:
        return self._capacity

    def acquire(self) -> int | None:
        with self._lock:
            for index in range(self._capacity):
                if index not in self._taken:
                    self._taken.add(index)
                    return index
            return None

    def release(self, index: int) -> None:
        with self._lock:
            self._taken.discard(index)

    def in_use(self) -> int:
        with self._lock:
            return len(self._taken)

    @contextmanager
    def hold(self, index: int) -> Iterator[int]:
        """Release `index` no matter how the cell ends — including an exception
        escaping the runner, which is the case that would otherwise leak a lane
        for the lifetime of the process."""
        try:
            yield index
        finally:
            self.release(index)
