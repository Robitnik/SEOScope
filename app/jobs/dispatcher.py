"""Dispatcher (PLAN §9.1): one lane per provider, running concurrently.

Supervises the lane threads — if one dies from an unhandled exception (it
shouldn't, `run_cell`'s guard should have already caught everything, but a
bug in the lane loop itself is exactly the case PLAN §9.2 calls out), it is
restarted so the other three lanes are never affected.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Any

from app.integrations.browser_session import SessionState
from app.jobs.lane import DEFAULT_AUTH_PAUSE_TIMEOUT_SECONDS, run_lane
from app.jobs.runner import DEFAULT_CELL_TIMEOUT_SECONDS
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
    ) -> None:
        self._session_scope = session_scope
        self._lease_owner = lease_owner
        self._providers = providers
        self._lease_ttl_seconds = lease_ttl_seconds
        self._cell_timeout_seconds = cell_timeout_seconds
        self._supervisor_interval_seconds = supervisor_interval_seconds
        self._check_session = check_session
        self._auth_pause_timeout_seconds = auth_pause_timeout_seconds
        self._stop_event = threading.Event()
        self._threads: dict[Provider, threading.Thread] = {}

    def _spawn(self, provider: Provider) -> threading.Thread:
        thread = threading.Thread(
            target=run_lane,
            args=(provider,),
            kwargs={
                "session_scope": self._session_scope,
                "lease_owner": self._lease_owner,
                "stop_event": self._stop_event,
                "lease_ttl_seconds": self._lease_ttl_seconds,
                "cell_timeout_seconds": self._cell_timeout_seconds,
                "check_session": self._check_session,
                "auth_pause_timeout_seconds": self._auth_pause_timeout_seconds,
            },
            name=f"lane-{provider.value}",
            daemon=True,
        )
        thread.start()
        return thread

    def run_forever(self) -> None:
        """Start every lane and supervise them until `stop()` is called.

        Blocks the calling thread. Intended to be called from the worker
        process's main thread (`app.jobs.worker`).
        """
        for provider in self._providers:
            self._threads[provider] = self._spawn(provider)
        logger.info("dispatcher_started lanes=%s", [p.value for p in self._providers])

        while not self._stop_event.is_set():
            for provider, thread in list(self._threads.items()):
                if not thread.is_alive() and not self._stop_event.is_set():
                    logger.error("lane_died_restarting provider=%s", provider.value)
                    self._threads[provider] = self._spawn(provider)
            self._stop_event.wait(self._supervisor_interval_seconds)

        logger.info("dispatcher_stopping")
        for thread in self._threads.values():
            thread.join(timeout=30)
        logger.info("dispatcher_stopped")

    def stop(self) -> None:
        self._stop_event.set()
