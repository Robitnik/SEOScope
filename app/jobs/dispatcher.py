"""Dispatcher (PLAN §9.1): lanes running concurrently, supervised.

One lane per provider was the original shape ("between services in parallel,
within a service sequential"). task-31 keeps the first half and softens the
second: a provider may now run N lanes at once on ONE logged-in profile — N
windows of the warm host Chrome, one per slot — because the profile lock became
a counting semaphore (`CellRepository.claim_next_cell(profile_capacity=...)`)
rather than a mutex.

The two things every lane of a provider must share live here and are handed
down: the `SlotPool` (which lane index, and therefore which browser window) and
the `AuthGate` (only one lane may wait for a login; the rest stop claiming).

Supervision is per THREAD, not per provider: one wedged lane is restarted while
its siblings keep collecting, which is the same "one failure never widens"
invariant PLAN §9.2 asks for one level down.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Any

from app.integrations.browser_session import SessionState
from app.jobs.auth_gate import AuthGate
from app.jobs.lane import DEFAULT_AUTH_PAUSE_TIMEOUT_SECONDS, run_lane
from app.jobs.runner import DEFAULT_CELL_TIMEOUT_SECONDS
from app.jobs.slots import SlotPool
from app.schemas.enums import Provider

logger = logging.getLogger(__name__)

SUPERVISOR_INTERVAL_SECONDS = 5.0


class Dispatcher:
    def __init__(
        self,
        *,
        session_scope: Callable[[], Any],
        lease_owner: str,
        providers: tuple[Provider, ...] = tuple(Provider),
        lease_ttl_seconds: int = 300,
        cell_timeout_seconds: int = DEFAULT_CELL_TIMEOUT_SECONDS,
        supervisor_interval_seconds: float = SUPERVISOR_INTERVAL_SECONDS,
        check_session: Callable[[Provider], SessionState] | None = None,
        auth_pause_timeout_seconds: float = DEFAULT_AUTH_PAUSE_TIMEOUT_SECONDS,
        parallelism: dict[Provider, int] | None = None,
        lane_start_stagger_seconds: float = 0.0,
    ) -> None:
        self._session_scope = session_scope
        self._lease_owner = lease_owner
        self._providers = providers
        self._lease_ttl_seconds = lease_ttl_seconds
        self._cell_timeout_seconds = cell_timeout_seconds
        self._supervisor_interval_seconds = supervisor_interval_seconds
        self._check_session = check_session
        self._auth_pause_timeout_seconds = auth_pause_timeout_seconds
        # Default 1 everywhere: a caller that says nothing gets exactly the
        # pre-task-31 dispatcher, one lane per provider.
        self._parallelism = {p: max(1, (parallelism or {}).get(p, 1)) for p in providers}
        self._lane_start_stagger_seconds = max(0.0, lane_start_stagger_seconds)
        self._slots = {p: SlotPool(self._parallelism[p]) for p in providers}
        self._gates = {p: AuthGate() for p in providers}
        self._stop_event = threading.Event()
        self._threads: dict[tuple[Provider, int], threading.Thread] = {}

    def _spawn(self, provider: Provider, index: int) -> threading.Thread:
        thread = threading.Thread(
            target=self._run_lane_staggered,
            args=(provider, index),
            kwargs={
                "session_scope": self._session_scope,
                "lease_owner": self._lease_owner,
                "stop_event": self._stop_event,
                "lease_ttl_seconds": self._lease_ttl_seconds,
                "cell_timeout_seconds": self._cell_timeout_seconds,
                "check_session": self._check_session,
                "auth_pause_timeout_seconds": self._auth_pause_timeout_seconds,
                "slots": self._slots[provider],
                "auth_gate": self._gates[provider],
            },
            # `index` names the THREAD, not the browser slot: slots are taken
            # per cell from the shared pool, so a restarted thread never
            # inherits (or leaks) the window its predecessor was using.
            name=f"lane-{provider.value}-{index}",
            daemon=True,
        )
        thread.start()
        return thread

    def _run_lane_staggered(self, provider: Provider, index: int, **kwargs: Any) -> None:
        """Delay lane `index` before its first claim (LIVE 2026-07-31).

        Without this, N lanes of one provider start in the same millisecond and
        drive N identical, heavy SPA navigations at once through a single
        Chrome process — the first live 3-lane Ahrefs run lost two of three
        cells to `Page.goto` timeouts that way, while the third succeeded.
        A few seconds apart is enough: after the first cell the lanes drift out
        of phase on their own, and nothing about correctness depends on it (the
        capacity semaphore is what bounds concurrency, not this delay).
        """
        if index and self._lane_start_stagger_seconds:
            if self._stop_event.wait(index * self._lane_start_stagger_seconds):
                return
        run_lane(provider, **kwargs)

    def run_forever(self) -> None:
        """Start every lane and supervise them until `stop()` is called.

        Blocks the calling thread. Intended to be called from the worker
        process's main thread (`app.jobs.worker`).
        """
        for provider in self._providers:
            for index in range(self._parallelism[provider]):
                self._threads[(provider, index)] = self._spawn(provider, index)
        logger.info(
            "dispatcher_started lanes=%s",
            {p.value: self._parallelism[p] for p in self._providers},
        )

        while not self._stop_event.is_set():
            for (provider, index), thread in list(self._threads.items()):
                if not thread.is_alive() and not self._stop_event.is_set():
                    logger.error("lane_died_restarting provider=%s index=%s", provider.value, index)
                    self._threads[(provider, index)] = self._spawn(provider, index)
            self._stop_event.wait(self._supervisor_interval_seconds)

        logger.info("dispatcher_stopping")
        for thread in self._threads.values():
            thread.join(timeout=30)
        logger.info("dispatcher_stopped")

    def stop(self) -> None:
        self._stop_event.set()
        # A lane parked behind a shut gate is waiting on an Event, not on
        # `stop_event`; opening every gate wakes them so shutdown is prompt.
        for gate in self._gates.values():
            gate.release()
