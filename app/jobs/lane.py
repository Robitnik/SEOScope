"""One lane per service (PLAN §9.1): claim-next-cell -> run-cell -> repeat,
strictly sequential within the lane. A dedicated OS thread per lane is what
gives "between services in parallel, within a service sequential": Python
threads release the GIL for the blocking I/O `run_cell` actually does
(network calls, browser automation, `time.sleep` in the fake adapter), so
several lanes genuinely overlap in wall-clock time even though only one
`claim_next_cell` call for a given provider is ever in flight at once.

task-21: when `run_cell` reports a cell `awaiting_auth` (its session died —
either the cheap pre-check or a live `collect()` discovered it), the lane
does not loop back to `claim_next_cell` — that would just skip the parked
cell and hand the next client a doomed attempt. Instead it parks here,
polling `check_session` until the operator logs back in (then requeues that
one cell and resumes exactly where it left off) or a timeout elapses (then
closes that cell's whole remaining backlog honestly instead of re-pausing
once per client).
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any

from app.db.repositories.cells import CellRepository
from app.db.repositories.jobs import JobRepository
from app.integrations.browser_session import SessionState
from app.jobs.runner import DEFAULT_CELL_TIMEOUT_SECONDS, CellHandle, assume_session_alive, run_cell
from app.schemas.enums import CellStatus, Provider

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 1.0

# How long a lane waits for the operator to log back in before giving up on
# the whole remaining queue for this provider (task-21: "таймаут очікування
# (напр. 15 хв)").
DEFAULT_AUTH_PAUSE_TIMEOUT_SECONDS = 15 * 60


class _MemoizedCheckSession:
    """Calls the real `check_session` at most once per lane-pass.

    `run_cell` itself always calls whatever `check_session` it's handed —
    that seam is what lets a live `collect()` catch a *mid-batch* expiry
    (PLAN §12, `runner.py`'s `result.error_code == "auth_required"` path).
    But the lane hands every cell *this* wrapper, not the raw function, so a
    healthy session is confirmed once and every other queued client in the
    same pass reuses that answer instead of re-probing (task-21: "один раз на
    провайдера, а не N разів на клієнтів"). `invalidate()` clears the cache
    after a pause resolves (resume or timeout), so whatever comes next gets a
    fresh read instead of trusting a stale one.
    """

    def __init__(self, real: Callable[[Provider], SessionState]) -> None:
        self._real = real
        self._cached: SessionState | None = None

    def __call__(self, provider: Provider) -> SessionState:
        if self._cached is None:
            self._cached = self._real(provider)
        return self._cached

    def invalidate(self) -> None:
        self._cached = None


def run_lane(
    provider: Provider,
    *,
    session_scope: Callable[[], Any],
    lease_owner: str,
    stop_event: threading.Event,
    lease_ttl_seconds: int = 300,
    poll_interval_seconds: float = POLL_INTERVAL_SECONDS,
    cell_timeout_seconds: int = DEFAULT_CELL_TIMEOUT_SECONDS,
    check_session: Callable[[Provider], SessionState] | None = None,
    auth_pause_timeout_seconds: float = DEFAULT_AUTH_PAUSE_TIMEOUT_SECONDS,
) -> None:
    """Blocking loop for one provider's lane; returns only once `stop_event`
    is set (graceful shutdown) or an unhandled exception escapes (the
    dispatcher supervises this and restarts the lane — PLAN §9.2).

    `check_session` defaults to `assume_session_alive` — see that function's
    docstring for why the real primitive is not the default here either.
    """
    raw_check = check_session or assume_session_alive
    memo = _MemoizedCheckSession(raw_check)

    while not stop_event.is_set():
        handle: CellHandle | None = None
        with session_scope() as session:
            cell = CellRepository(session).claim_next_cell(
                provider, lease_owner=lease_owner, lease_ttl_seconds=lease_ttl_seconds
            )
            if cell is not None:
                handle = CellHandle(
                    cell_id=cell.id, job_id=cell.job_id, client_id=cell.client_id, provider=provider
                )

        if handle is None:
            stop_event.wait(poll_interval_seconds)
            continue

        try:
            status = run_cell(
                handle,
                session_scope=session_scope,
                lease_owner=lease_owner,
                lease_ttl_seconds=lease_ttl_seconds,
                cell_timeout_seconds=cell_timeout_seconds,
                check_session=memo,
            )
        except Exception:  # noqa: BLE001 - run_cell already guards; belt-and-suspenders only
            logger.exception(
                "lane_run_cell_raised_unexpectedly provider=%s cell_id=%s", provider, handle.cell_id
            )
            continue

        if status == CellStatus.awaiting_auth:
            # The pause/resume poll always talks to the *real* check, live —
            # only the "already confirmed alive this pass" fast path is cached.
            _await_auth_and_resume(
                handle,
                provider,
                check_session=raw_check,
                session_scope=session_scope,
                stop_event=stop_event,
                poll_interval_seconds=poll_interval_seconds,
                timeout_seconds=auth_pause_timeout_seconds,
            )
            memo.invalidate()


def _await_auth_and_resume(
    handle: CellHandle,
    provider: Provider,
    *,
    check_session: Callable[[Provider], SessionState],
    session_scope: Callable[[], Any],
    stop_event: threading.Event,
    poll_interval_seconds: float,
    timeout_seconds: float,
) -> None:
    """Park the lane on one `awaiting_auth` cell: no `claim_next_cell` calls
    happen while this runs, so no other client is attempted against the same
    dead session. The cell already left `running` (task-21, `runner.py`),
    which by construction frees its profile lock (`acquire_profile_lock` only
    counts `running` cells) and stops its lease clock (`expired_cells` only
    watches `running` too) — no separate lock-release or heartbeat code is
    needed here.
    """
    deadline = time.monotonic() + timeout_seconds
    logger.warning(
        "lane_paused_awaiting_auth provider=%s cell_id=%s timeout_seconds=%s",
        provider.value,
        handle.cell_id,
        timeout_seconds,
    )

    while not stop_event.is_set():
        if check_session(provider) == SessionState.alive:
            with session_scope() as session:
                CellRepository(session).set_status(
                    handle.cell_id, CellStatus.queued, error_code=None, error_message=None
                )
            logger.info(
                "lane_resumed_after_login provider=%s cell_id=%s", provider.value, handle.cell_id
            )
            return

        if time.monotonic() >= deadline:
            with session_scope() as session:
                cells = CellRepository(session)
                jobs = JobRepository(session)
                affected_job_ids = {handle.job_id}

                cells.set_status(
                    handle.cell_id,
                    CellStatus.failed,
                    error_code="auth_required",
                    error_message="login not completed before timeout",
                )
                # The rest of this provider's queue is behind the same dead
                # session: close it out now, honestly, rather than each cell
                # re-pausing for another full `timeout_seconds` in turn.
                backlog = cells.queued_cells_for_provider(provider)
                for cell in backlog:
                    cells.set_status(
                        cell.id,
                        CellStatus.failed,
                        error_code="auth_required",
                        error_message="upstream session still expired after auth-pause timeout",
                    )
                    affected_job_ids.add(cell.job_id)

                for job_id in affected_job_ids:
                    jobs.sync_status_from_cells(job_id)

            logger.warning(
                "lane_auth_pause_timed_out provider=%s cell_id=%s closed_backlog=%d",
                provider.value,
                handle.cell_id,
                len(backlog),
            )
            return

        stop_event.wait(poll_interval_seconds)
