"""`SlotPool` — which parallel lane a cell runs in (task-31).

The pool is the only thing that decides which browser WINDOW a collection
drives, so handing the same index to two cells at once would put two lanes in
one window — exactly the failure the slot concept exists to prevent.
"""

from __future__ import annotations

import threading

import pytest

from app.jobs.slots import SlotPool


def test_hands_out_distinct_indexes_up_to_capacity():
    pool = SlotPool(3)

    taken = [pool.acquire() for _ in range(3)]

    assert sorted(taken) == [0, 1, 2]
    assert pool.in_use() == 3


def test_returns_none_when_full_instead_of_blocking():
    pool = SlotPool(1)
    pool.acquire()

    assert pool.acquire() is None


def test_released_slot_is_reused():
    pool = SlotPool(2)
    first = pool.acquire()
    pool.acquire()

    pool.release(first)

    assert pool.acquire() == first


def test_hold_releases_even_when_the_body_raises():
    """A cell that blows up must not take its lane with it — otherwise one
    unexpected exception permanently narrows the worker's parallelism."""
    pool = SlotPool(1)
    slot = pool.acquire()

    with pytest.raises(RuntimeError), pool.hold(slot):
        raise RuntimeError("cell exploded")

    assert pool.in_use() == 0
    assert pool.acquire() == slot


def test_concurrent_acquire_never_hands_out_a_duplicate():
    pool = SlotPool(4)
    results: list[int | None] = []
    lock = threading.Lock()
    start = threading.Barrier(16)

    def worker() -> None:
        start.wait()
        got = pool.acquire()
        with lock:
            results.append(got)

    threads = [threading.Thread(target=worker) for _ in range(16)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    handed_out = [value for value in results if value is not None]
    assert sorted(handed_out) == [0, 1, 2, 3], "a slot was handed to two lanes at once"
    assert results.count(None) == 12


def test_capacity_must_be_at_least_one():
    with pytest.raises(ValueError, match="capacity"):
        SlotPool(0)
