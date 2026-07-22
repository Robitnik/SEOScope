"""Execute one `(client, provider)` cell end-to-end (PLAN §9.2/§9.3).

`run_cell` is the "zero crashes" boundary: preflight -> (not_configured?) ->
collect -> normalize -> validate -> short atomic persist, with a guard around
the *entire* sequence so that literally any exception, timeout or garbage
adapter return value becomes a recorded `failed` cell instead of taking down
the lane, the process, or any other cell. Network/browser work (`collect()`)
runs with no DB session open; only the short read/write steps around it touch
the database, per PLAN §5.1's "one writer, short transactions" invariant.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from app.db.repositories.cells import CellRepository
from app.db.repositories.clients import ClientRepository
from app.db.repositories.jobs import JobRepository
from app.db.repositories.metrics import MetricRepository
from app.db.repositories.snapshots import SnapshotRepository
from app.integrations.browser_session import BROWSER_PROVIDERS, SessionState
from app.integrations.registry import get_adapter
from app.jobs.events import append_cell_status_event, make_emitter
from app.metrics import CATALOG, normalize_result, resolve_periods
from app.schemas.adapter import CollectionContext, CollectionResult
from app.schemas.enums import CellStatus, PeriodRole, Provider, Quality

logger = logging.getLogger(__name__)

# `collect()` has no cooperative-cancellation guarantee from a hung/misbehaving
# adapter (browser wedged, network stalled forever); this wall-clock ceiling
# is what keeps one bad cell from blocking its lane "forever" (PLAN §9.2).
# True per-*stage* deadlines would need a deadline field on `CollectionContext`
# (a task-02 contract change) or adapter-side self-policing — out of scope
# here; see this task's Completion report follow-ups.
DEFAULT_CELL_TIMEOUT_SECONDS = 20 * 60

# Quality outcomes that count as "the attempt genuinely completed" for the
# purposes of `SnapshotRepository.latest_success` freshness checks below —
# mirrors that repository's own `_SUCCESS_QUALITIES`.
_FRESH_QUALITIES = (Quality.complete, Quality.partial, Quality.truncated, Quality.stale)


def assume_session_alive(_provider: Provider) -> SessionState:
    """Default `check_session` used when no lane/caller supplies one.

    Deliberately a no-op, not `app.integrations.browser_session.check_session`:
    `run_cell`/`run_lane` are exercised directly by tests with `Provider.ahrefs`/
    `semrush` (browser providers) and no warm Chrome in the test sandbox — the
    real primitive would report every one of those sessions `expired` and turn
    every existing green test into a spurious auth-pause. The real check is
    wired in exactly one place: `app.jobs.worker.main`'s `Dispatcher(...)` call.
    """
    return SessionState.alive


@dataclass(frozen=True)
class CellHandle:
    """Everything the runner needs about a claimed cell — a plain snapshot of
    the ORM row taken right after `claim_next_cell`, so the runner never
    holds that row's session open across the network/browser call.
    """

    cell_id: str
    job_id: str
    client_id: str
    provider: Provider


def _safe_error(exc: BaseException) -> tuple[str, str]:
    """Map an arbitrary exception to a safe `(error_code, error_message)` pair
    — never the raw exception text, which could embed a URL, header or path
    that leaks something sensitive (PLAN §13).
    """
    return type(exc).__name__, "collection failed; see server logs for detail"


def _validate_metrics(metrics: list) -> list[str]:  # type: ignore[type-arg]
    """Defense-in-depth structural check (PLAN §12): never drops a metric —
    an unknown/mismatched metric is still stored — but flags anomalies as
    warnings so they surface on the snapshot instead of silently passing.
    """
    warnings: list[str] = []
    for m in metrics:
        spec = CATALOG.get(m.metric_key)
        if spec is None:
            continue
        if spec.comparison_kind == "range" and m.period_role == PeriodRole.snapshot:
            warnings.append(f"{m.metric_key}: snapshot period_role on a range metric")
        if spec.comparison_kind == "snapshot" and m.period_role != PeriodRole.snapshot:
            warnings.append(f"{m.metric_key}: non-snapshot period_role on a snapshot metric")
    return warnings


def _map_cell_status(quality: Quality) -> CellStatus:
    if quality == Quality.complete:
        return CellStatus.succeeded
    # partial/truncated/stale/unavailable: attempt completed, but something
    # about the data is incomplete or absent — never a hard failure, and
    # never silently indistinguishable from `succeeded` (PLAN §12).
    return CellStatus.partial


def _is_fresh_enough(snapshot: Any, current_end: date) -> bool:
    reference: date = snapshot.data_through or snapshot.observed_at.date()
    return reference >= current_end


def run_cell(
    handle: CellHandle,
    *,
    session_scope: Callable[[], Any],
    lease_owner: str,
    lease_ttl_seconds: int = 300,
    cell_timeout_seconds: int = DEFAULT_CELL_TIMEOUT_SECONDS,
    check_session: Callable[[Provider], SessionState] | None = None,
) -> CellStatus:
    """Run preflight -> collect -> normalize -> validate -> persist for one
    cell, guarded end-to-end. Returns the final `CellStatus` (also always
    persisted) — the caller (the lane) never needs to inspect exceptions.

    `check_session` defaults to `assume_session_alive` (see its docstring for
    why that, and not the real primitive, is the default).
    """
    try:
        return _run_cell_unguarded(
            handle,
            session_scope=session_scope,
            lease_owner=lease_owner,
            lease_ttl_seconds=lease_ttl_seconds,
            cell_timeout_seconds=cell_timeout_seconds,
            check_session=check_session or assume_session_alive,
        )
    except Exception as exc:  # noqa: BLE001 - the "zero crashes" boundary
        error_code, error_message = _safe_error(exc)
        logger.exception(
            "cell_failed_unexpectedly cell_id=%s provider=%s", handle.cell_id, handle.provider
        )
        try:
            with session_scope() as session:
                CellRepository(session).set_status(
                    handle.cell_id,
                    CellStatus.failed,
                    error_code=error_code,
                    error_message=error_message,
                )
                append_cell_status_event(
                    session,
                    job_id=handle.job_id,
                    provider=handle.provider,
                    status=CellStatus.failed,
                    error_code=error_code,
                    message=error_message,
                )
                JobRepository(session).sync_status_from_cells(handle.job_id)
        except Exception:  # noqa: BLE001 - persisting the failure must not itself crash the lane
            logger.exception("cell_failed_to_record_failure cell_id=%s", handle.cell_id)
        return CellStatus.failed


def _run_cell_unguarded(
    handle: CellHandle,
    *,
    session_scope: Callable[[], Any],
    lease_owner: str,
    lease_ttl_seconds: int,
    cell_timeout_seconds: int,
    check_session: Callable[[Provider], SessionState],
) -> CellStatus:
    cell_repo_read: dict[str, Any] = {}
    with session_scope() as session:
        client = ClientRepository(session).get(handle.client_id)
        integration = ClientRepository(session).get_integration(handle.client_id, handle.provider)
        cell = CellRepository(session).get(handle.cell_id)
        job = JobRepository(session).get(handle.job_id)
        if client is None or cell is None or job is None:
            raise LookupError(f"cell/client/job vanished mid-run: {handle.cell_id}")
        cell_repo_read = {
            "timezone": client.timezone,
            "period_kind": job.period_kind,
            "comparison_mode": job.comparison_mode,
            "anchor_date": job.anchor_date,
            "mode": job.mode,
            "integration_enabled": integration.enabled if integration else False,
            "integration_config": dict(integration.config_json or {}) if integration else {},
            "source_mode": integration.source_mode if integration else None,
        }

    if integration is None or not cell_repo_read["integration_enabled"]:
        return _terminate(
            session_scope, handle, CellStatus.not_configured, error_code="not_configured"
        )

    if handle.provider in BROWSER_PROVIDERS:
        state = check_session(handle.provider)
        if state != SessionState.alive:
            # Cheap probe only (task-20) — no browser is opened for this cell.
            # The lane (task-21) parks here instead of claiming the next
            # client, so a dead session is discovered once, not once per
            # client on the queue.
            return _terminate(
                session_scope,
                handle,
                CellStatus.awaiting_auth,
                error_code="auth_required",
                error_message=f"{handle.provider.value} session {state.value}; login required",
            )

    anchor_dt = datetime(
        cell_repo_read["anchor_date"].year,
        cell_repo_read["anchor_date"].month,
        cell_repo_read["anchor_date"].day,
        12,
        tzinfo=ZoneInfo(cell_repo_read["timezone"]),
    )
    periods = resolve_periods(
        cell_repo_read["period_kind"],
        anchor_dt,
        tz=cell_repo_read["timezone"],
        mode=cell_repo_read["comparison_mode"],
    )

    context = CollectionContext(
        client_id=handle.client_id,
        provider=handle.provider,
        source_mode=cell_repo_read["source_mode"],
        period_kind=cell_repo_read["period_kind"],
        comparison_mode=cell_repo_read["comparison_mode"],
        current=periods.current,
        previous=periods.previous,
        timezone=cell_repo_read["timezone"],
        force_refresh=cell_repo_read["mode"] == "force_refresh",
        integration_config=cell_repo_read["integration_config"],
    )

    try:
        adapter = get_adapter(handle.provider)
    except LookupError as exc:
        return _terminate(
            session_scope,
            handle,
            CellStatus.failed,
            error_code="adapter_not_registered",
            error_message=str(exc),
        )

    try:
        preflight = adapter.preflight(context)
    except Exception as exc:  # noqa: BLE001 - adapter preflight must never crash the cell
        return _terminate(
            session_scope,
            handle,
            CellStatus.not_configured,
            error_code="preflight_error",
            error_message=_safe_error(exc)[0],
        )

    if not preflight.ok:
        # `auth_required` is configured-but-dead-session, never "never set up"
        # (PLAN §9.2) — some adapters (e.g. Semrush API mode) already report
        # this reason code from `preflight()` itself, ahead of `collect()`.
        status = (
            CellStatus.awaiting_auth
            if preflight.reason_code == "auth_required"
            else CellStatus.not_configured
        )
        return _terminate(
            session_scope,
            handle,
            status,
            error_code=preflight.reason_code or "not_configured",
            error_message=preflight.message,
        )

    if not context.force_refresh:
        with session_scope() as session:
            latest = SnapshotRepository(session).latest_success(handle.client_id, handle.provider)
        if latest is not None and _is_fresh_enough(latest, periods.current.end):
            return _terminate(session_scope, handle, CellStatus.skipped, error_code=None)

    emit = make_emitter(
        session_scope,
        job_id=handle.job_id,
        cell_id=handle.cell_id,
        lease_owner=lease_owner,
        lease_ttl_seconds=lease_ttl_seconds,
    )
    is_cancel_requested = _make_cancel_checker(session_scope, handle.cell_id)

    result = _collect_with_timeout(
        adapter, context, emit, is_cancel_requested, cell_timeout_seconds
    )
    if result is _TIMEOUT:
        return _terminate(session_scope, handle, CellStatus.failed, error_code="timeout")
    if result is _CANCELLED:
        return _terminate(session_scope, handle, CellStatus.cancelled, error_code=None)
    assert isinstance(result, CollectionResult)

    metrics = normalize_result(result)
    validation_warnings = _validate_metrics(metrics)
    # A live `collect()` discovering a dead session mid-batch (e.g. Semrush's
    # `SessionExpired` -> `error_code="auth_required"`) parks the cell the
    # same way the cheap pre-check above does, instead of collapsing into
    # `partial` and losing the signal (PLAN §12 — this was defect #1).
    final_status = (
        CellStatus.awaiting_auth
        if result.error_code == "auth_required"
        else _map_cell_status(result.quality)
    )

    with session_scope() as session:
        snapshot = SnapshotRepository(session).create_immutable(
            job_id=handle.job_id,
            client_id=handle.client_id,
            provider=handle.provider,
            source_mode=context.source_mode,
            observed_at=result.observed_at,
            quality=result.quality,
            data_through=result.data_through,
            warnings=[*result.warnings, *validation_warnings],
            provenance=result.provenance,
        )
        MetricRepository(session).bulk_insert(
            snapshot_id=snapshot.id,
            client_id=handle.client_id,
            provider=handle.provider,
            values=metrics,
        )
        CellRepository(session).set_status(
            handle.cell_id, final_status, error_code=result.error_code, error_message=None
        )
        append_cell_status_event(
            session,
            job_id=handle.job_id,
            provider=handle.provider,
            status=final_status,
            error_code=result.error_code,
        )
        JobRepository(session).sync_status_from_cells(handle.job_id)
    return final_status


_TIMEOUT = object()
_CANCELLED = object()


def _collect_with_timeout(
    adapter: Any,
    context: CollectionContext,
    emit: Callable[..., None],
    is_cancel_requested: Callable[[], bool],
    timeout_seconds: int,
) -> Any:
    from app.jobs.fake_adapter import CancelledByAdapter

    # A plain daemon thread, not `ThreadPoolExecutor`: its `__exit__`/
    # `shutdown()` joins outstanding workers, which would block this call
    # for the *entire* hung duration on timeout — defeating the timeout
    # itself. A daemon thread can be abandoned outright: the interpreter
    # does not wait for it to exit, so a genuinely wedged adapter (real
    # browser/network hang) never blocks worker shutdown either.
    outcome: dict[str, Any] = {}

    def _run() -> None:
        try:
            outcome["result"] = adapter.collect(context, emit, is_cancel_requested)
        except BaseException as exc:  # noqa: BLE001 - relayed to the caller's thread below
            outcome["exc"] = exc

    thread = threading.Thread(target=_run, name="cell-collect", daemon=True)
    thread.start()
    thread.join(timeout=timeout_seconds)
    if thread.is_alive():
        return _TIMEOUT
    if "exc" in outcome:
        exc = outcome["exc"]
        if isinstance(exc, CancelledByAdapter):
            return _CANCELLED
        raise exc
    return outcome["result"]


def _make_cancel_checker(session_scope: Callable[[], Any], cell_id: str) -> Callable[[], bool]:
    def _check() -> bool:
        try:
            with session_scope() as session:
                cell = CellRepository(session).get(cell_id)
                return cell is not None and cell.status == CellStatus.cancel_requested
        except Exception:  # noqa: BLE001 - a failed poll must never abort collection
            return False

    return _check


def _terminate(
    session_scope: Callable[[], Any],
    handle: CellHandle,
    status: CellStatus,
    *,
    error_code: str | None,
    error_message: str | None = None,
) -> CellStatus:
    with session_scope() as session:
        CellRepository(session).set_status(
            handle.cell_id, status, error_code=error_code, error_message=error_message
        )
        # Same transaction as the status change: a terminal/parked cell that
        # never reached `collect()` has no other way onto the SSE stream, and
        # without a row here the sync modal never re-reads this status.
        append_cell_status_event(
            session,
            job_id=handle.job_id,
            provider=handle.provider,
            status=status,
            error_code=error_code,
            message=error_message,
        )
        JobRepository(session).sync_status_from_cells(handle.job_id)
    return status
