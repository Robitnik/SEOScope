"""One auth pause per provider, however many lanes it runs (task-31).

With a single lane per provider, `run_lane` could simply park on the cell whose
session turned out dead (task-21): no other lane existed to hand the next
client a doomed attempt. With N lanes that invariant breaks in three ways at
once — N lanes would each park on their own cell, each ask the operator to log
in, and each independently close "the rest of the queue" on timeout, racing one
another over the same backlog.

`AuthGate` restores the original guarantee without collapsing the parallelism:
exactly ONE lane becomes the pauser and does the polling and the backlog
close-out; every other lane of that provider stops claiming while the gate is
shut, but the cells they are already running finish normally (they hold live
sessions — killing them would throw away work that is succeeding).
"""

from __future__ import annotations

import threading


class AuthGate:
    """Open by default. `try_become_pauser()` shuts it for everyone."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._open = threading.Event()
        self._open.set()
        self._pauser: int | None = None

    @property
    def is_open(self) -> bool:
        return self._open.is_set()

    def try_become_pauser(self, slot: int) -> bool:
        """True if this lane is the one that will wait for the login.

        False means another lane got there first — the caller should leave its
        own cell alone (it is already parked in `awaiting_auth`, which the
        pauser's resume will requeue) and simply stop claiming until the gate
        reopens.
        """
        with self._lock:
            if self._pauser is not None:
                return False
            self._pauser = slot
            self._open.clear()
            return True

    def release(self) -> None:
        """Reopen the gate (login restored, or the wait timed out and the
        backlog was closed). Safe to call from a lane that is not the pauser —
        it is a no-op then, so a shutdown path can always call it."""
        with self._lock:
            self._pauser = None
            self._open.set()

    def wait_until_open(self, timeout: float) -> bool:
        """Block up to `timeout` seconds; True if the gate is open on return."""
        return self._open.wait(timeout)


class AuthGateRegistry:
    """One gate per provider, created on first use."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._gates: dict[str, AuthGate] = {}

    def get(self, key: str) -> AuthGate:
        with self._lock:
            gate = self._gates.get(key)
            if gate is None:
                gate = AuthGate()
                self._gates[key] = gate
            return gate
