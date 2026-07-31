"""`AuthGate` — one auth pause per provider, however many lanes (task-31).

With N lanes per provider, the task-21 guarantee ("a dead session is
discovered once, not once per client") only survives if exactly one lane waits
for the login while the others stop claiming. These tests pin that down.
"""

from __future__ import annotations

import threading

from app.jobs.auth_gate import AuthGate, AuthGateRegistry


def test_starts_open():
    assert AuthGate().is_open is True


def test_only_one_lane_becomes_the_pauser():
    gate = AuthGate()

    assert gate.try_become_pauser(0) is True
    assert gate.try_become_pauser(1) is False
    assert gate.try_become_pauser(2) is False


def test_pausing_shuts_the_gate_for_every_lane():
    gate = AuthGate()
    gate.try_become_pauser(0)

    assert gate.is_open is False

    gate.release()

    assert gate.is_open is True


def test_gate_can_be_taken_again_after_release():
    """A second expiry later in the same run must be able to pause again —
    otherwise the first login would permanently disarm the protection."""
    gate = AuthGate()
    gate.try_become_pauser(0)
    gate.release()

    assert gate.try_become_pauser(1) is True


def test_release_from_a_non_pauser_is_a_safe_no_op():
    """Shutdown releases every gate blindly so parked lanes wake up; that must
    not corrupt a gate nobody is holding."""
    gate = AuthGate()

    gate.release()

    assert gate.is_open is True


def test_waiting_lane_wakes_when_the_gate_reopens():
    gate = AuthGate()
    gate.try_become_pauser(0)
    woke = threading.Event()

    def waiter() -> None:
        if gate.wait_until_open(timeout=5):
            woke.set()

    thread = threading.Thread(target=waiter)
    thread.start()
    assert not woke.wait(0.1), "lane returned before the login was resolved"

    gate.release()
    thread.join(timeout=5)

    assert woke.is_set()


def test_wait_times_out_while_the_gate_stays_shut():
    gate = AuthGate()
    gate.try_become_pauser(0)

    assert gate.wait_until_open(timeout=0.05) is False


def test_registry_gives_one_gate_per_key():
    registry = AuthGateRegistry()

    assert registry.get("ahrefs") is registry.get("ahrefs")
    assert registry.get("ahrefs") is not registry.get("semrush")


def test_concurrent_pause_attempts_pick_exactly_one_winner():
    gate = AuthGate()
    start = threading.Barrier(8)
    winners: list[int] = []
    lock = threading.Lock()

    def worker(slot: int) -> None:
        start.wait()
        if gate.try_become_pauser(slot):
            with lock:
                winners.append(slot)

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(winners) == 1
