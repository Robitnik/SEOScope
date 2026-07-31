"""One lane (PLAN §9.1): claim-next-cell -> run-cell -> repeat, strictly
sequential inside this lane. A dedicated OS thread per lane is what gives
"services overlap in wall-clock time": Python threads release the GIL for the
blocking I/O `run_cell` actually does (network calls, browser automation,
`time.sleep` in the fake adapter).

task-31: a provider may now run SEVERAL lanes at once — one per slot, i.e. one
per window of the single logged-in warm Chrome profile. Each lane still does
one cell at a time; what changed is that "within a service sequential" became
"within a service, at most `capacity` at a time", enforced in the claim itself
(`CellRepository.claim_next_cell(profile_capacity=...)`) rather than by there
being only one thread. Lanes of one provider share exactly two things, both
passed in by the dispatcher: the `SlotPool` (which window) and the `AuthGate`
(who waits for a login).

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
from app.jobs.auth_gate import AuthGate
from app.jobs.events import append_cell_status_event
from app.jobs.runner import DEFAULT_CELL_TIMEOUT_SECONDS, CellHandle, assume_session_alive, run_cell
from app.jobs.slots import SlotPool
from app.schemas.enums import CellStatus, Provider

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 1.0

# How long a lane waits for the operator to log back in before giving up on
# the whole remaining queue for this provider (task-21: "wait timeout
# (e.g. 15 min)").
DEFAULT_AUTH_PAUSE_TIMEOUT_SECONDS = 15 * 60


class _MemoizedCheckSession:
    """Calls the real `check_session` at most once per lane-pass.

    `run_cell` itself always calls whatever `check_session` it's handed —
    that seam is what lets a live `collect()` catch a *mid-batch* expiry
    (PLAN §12, `runner.py`'s `result.error_code == "auth_required"` path).
    But the lane hands every cell *this* wrapper, not the raw function, so a
    healthy session is confirmed once and every other queued client in the
    same pass reuses that answer instead of re-probing (task-21: "once per
    provider, not N times per client"). `invalidate()` clears the cache
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
    slots: SlotPool | None = None,
    auth_gate: AuthGate | None = None,
) -> None:
    """Blocking loop for one lane of one provider; returns only once
    `stop_event` is set (graceful shutdown) or an unhandled exception escapes
    (the dispatcher supervises this and restarts the lane — PLAN §9.2).

    `check_session` defaults to `assume_session_alive` — see that function's
    docstring for why the real primitive is not the default here either.

    task-31: `slots`/`auth_gate` are what let SEVERAL lanes of one provider run
    at once. The slot index is both the DB capacity unit and the browser-window
    identity handed to the adapter; the gate makes sure a dead session is
    waited on by exactly one of those lanes. Both default to the single-lane
    behaviour, so a caller that passes neither gets task-21's lane unchanged.
    """
    raw_check = check_session or assume_session_alive
    memo = _MemoizedCheckSession(raw_check)
    pool = slots or SlotPool(1)
    gate = auth_gate or AuthGate()

    while not stop_event.is_set():
        # A shut gate means another lane of this provider is waiting for the
        # operator to log in. Claiming now would just hand the next client the
        # same dead session — the exact failure task-21 removed.
        if not gate.is_open:
            gate.wait_until_open(poll_interval_seconds)
            continue

        slot = pool.acquire()
        if slot is None:
            # Every lane of this profile is busy. Not an error: the DB claim
            # would refuse too, this only avoids asking.
            stop_event.wait(poll_interval_seconds)
            continue

        with pool.hold(slot):
            handle: CellHandle | None = None
            with session_scope() as session:
                cell = CellRepository(session).claim_next_cell(
                    provider,
                    lease_owner=lease_owner,
                    lease_ttl_seconds=lease_ttl_seconds,
                    profile_capacity=pool.capacity,
                    slot=slot,
                )
                if cell is not None:
                    handle = CellHandle(
                        cell_id=cell.id,
                        job_id=cell.job_id,
                        client_id=cell.client_id,
                        provider=provider,
                        slot=slot,
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
                    "lane_run_cell_raised_unexpectedly provider=%s slot=%s cell_id=%s",
                    provider,
                    slot,
                    handle.cell_id,
                )
                continue

            if status == CellStatus.awaiting_auth:
                if not gate.try_become_pauser(slot):
                    # Another lane is already waiting for this provider's login.
                    # Ours stays parked in `awaiting_auth`; the pauser's resume
                    # requeues every such cell, and its timeout closes them.
                    logger.info(
                        "lane_auth_pause_delegated provider=%s slot=%s cell_id=%s",
                        provider.value,
                        slot,
                        handle.cell_id,
                    )
                    continue
                try:
                    # The pause/resume poll always talks to the *real* check,
                    # live — only the "already confirmed alive this pass" fast
                    # path is cached.
                    _await_auth_and_resume(
                        handle,
                        provider,
                        check_session=raw_check,
                        session_scope=session_scope,
                        stop_event=stop_event,
                        poll_interval_seconds=poll_interval_seconds,
                        timeout_seconds=auth_pause_timeout_seconds,
                    )
                finally:
                    gate.release()
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
        # A parked lane is not looping through `claim_next_cell`, so this is
        # the only place that can notice someone else moving this cell while
        # we wait — most importantly an operator pressing «Stop», which
        # flags it `cancel_requested`. Without this re-read the cancel was
        # never honoured and the cell stayed non-terminal forever, so the job
        # could never finish (and neither could its SSE stream).
        with session_scope() as session:
            cells = CellRepository(session)
            cell = cells.get(handle.cell_id)
            if cell is None:
                return
            if cell.status == CellStatus.cancel_requested:
                cells.set_status(
                    handle.cell_id, CellStatus.cancelled, error_code=None, error_message=None
                )
                append_cell_status_event(
                    session,
                    job_id=handle.job_id,
                    provider=provider,
                    status=CellStatus.cancelled,
                )
                JobRepository(session).sync_status_from_cells(handle.job_id)
                logger.info(
                    "lane_auth_pause_cancelled provider=%s cell_id=%s",
                    provider.value,
                    handle.cell_id,
                )
                return
            if cell.status != CellStatus.awaiting_auth:
                # Requeued by retry/recovery elsewhere — stop parking and let
                # the normal claim loop pick it back up.
                logger.info(
                    "lane_auth_pause_released provider=%s cell_id=%s status=%s",
                    provider.value,
                    handle.cell_id,
                    cell.status.value,
                )
                return

        if check_session(provider) == SessionState.alive:
            with session_scope() as session:
                cells = CellRepository(session)
                jobs = JobRepository(session)
                # Every cell parked on this provider's session, not just ours:
                # with several lanes (task-31) more than one can be waiting on
                # the same login, and only the lane holding the auth gate gets
                # here — so anything it leaves parked would stay parked forever.
                parked = cells.awaiting_auth_cells(provider)
                for cell in parked:
                    cells.set_status(
                        cell.id, CellStatus.queued, error_code=None, error_message=None
                    )
                    append_cell_status_event(
                        session,
                        job_id=cell.job_id,
                        provider=provider,
                        status=CellStatus.queued,
                        message="session restored; cell requeued",
                    )
                for job_id in {cell.job_id for cell in parked}:
                    jobs.sync_status_from_cells(job_id)
            logger.info(
                "lane_resumed_after_login provider=%s cell_id=%s requeued=%d",
                provider.value,
                handle.cell_id,
                len(parked),
            )
            return

        if time.monotonic() >= deadline:
            with session_scope() as session:
                cells = CellRepository(session)
                jobs = JobRepository(session)
                affected_job_ids = {handle.job_id}

                # Read before writing: this returns our own cell too, plus any
                # cell another lane parked on the same dead session (task-31).
                for cell in cells.awaiting_auth_cells(provider):
                    cells.set_status(
                        cell.id,
                        CellStatus.failed,
                        error_code="auth_required",
                        error_message="login not completed before timeout",
                    )
                    append_cell_status_event(
                        session,
                        job_id=cell.job_id,
                        provider=provider,
                        status=CellStatus.failed,
                        error_code="auth_required",
                        message="login not completed before timeout",
                    )
                    affected_job_ids.add(cell.job_id)
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
                    append_cell_status_event(
                        session,
                        job_id=cell.job_id,
                        provider=provider,
                        status=CellStatus.failed,
                        error_code="auth_required",
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
