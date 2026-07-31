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
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from app.db.repositories.analytics_datasets import (
    AnalyticsDatasetRepository,
    AnalyticsDatasetRowInput,
)
from app.db.repositories.cells import CellRepository
from app.db.repositories.clients import ClientRepository
from app.db.repositories.dimensions import DimensionRepository, DimensionRowInput
from app.db.repositories.jobs import JobRepository
from app.db.repositories.metrics import MetricRepository
from app.db.repositories.snapshots import SnapshotRepository
from app.integrations.browser_session import BROWSER_PROVIDERS, SessionState
from app.integrations.registry import get_adapter
from app.jobs.events import append_cell_status_event, make_emitter
from app.metrics import CATALOG, normalize_result, resolve_periods
from app.schemas.adapter import CollectionContext, CollectionResult
from app.schemas.enums import CellStatus, PeriodRole, Provider, Quality, SourceMode

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
    # Which parallel lane claimed this cell (task-31); `0` when the provider
    # runs a single lane, which is every provider until the dispatcher is
    # configured otherwise.
    slot: int = 0


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
    if quality in (Quality.complete, Quality.truncated):
        return CellStatus.succeeded
    # partial/stale/unavailable: attempt completed, but something
    # about the data is incomplete or absent — never a hard failure, and
    # never silently indistinguishable from `succeeded` (PLAN §12).
    return CellStatus.partial


def _is_fresh_enough(snapshot: Any, current_end: date) -> bool:
    reference: date = snapshot.data_through or snapshot.observed_at.date()
    return reference >= current_end


def _includes_gsc_dimensions(snapshot: Any) -> bool:
    """Whether a GSC snapshot came from the dimension-aware worker path.

    Older snapshots only contain aggregate metrics.  Treating one of those as
    fully fresh would keep page/query tables empty forever in ``collect_missing``
    mode, even though Reporting Ninja can now provide those rows.
    """
    dimensions = (snapshot.provenance_json or {}).get("dimensions", {})
    return bool(dimensions.get("attempted"))


def _includes_analytics_datasets(snapshot: Any) -> bool:
    datasets = (snapshot.provenance_json or {}).get("analytics_datasets", {})
    return bool(datasets.get("attempted") and datasets.get("row_count", 0))


def _combine_quality(primary: Quality, secondary: Quality) -> Quality:
    if primary == secondary:
        return primary
    qualities = {primary, secondary}
    if Quality.partial in qualities:
        return Quality.partial
    if Quality.unavailable in qualities:
        return Quality.partial
    if Quality.stale in qualities:
        return Quality.stale
    if Quality.truncated in qualities:
        return Quality.truncated
    return Quality.partial


def _combine_gsc_dimension_quality(primary: Quality, dimension_result: Any) -> Quality:
    """Do not mistake the intentional top-N detail view for source loss.

    GSC page/query rows are fully fetched from Reporting Ninja, then capped for
    dashboard storage with the remainder aggregated into ``Other``. That cut
    remains visible in dimensional-row quality/provenance, but it must not turn
    complete aggregate KPIs into a provider-wide ``truncated`` result. A real
    Reporting Ninja ``max_rows`` truncation still downgrades the snapshot.
    """
    if (
        dimension_result.quality == Quality.truncated
        and dimension_result.provenance.get("source_truncated") is False
        and dimension_result.provenance.get("top_n_truncated") is True
    ):
        return primary
    return _combine_quality(primary, dimension_result.quality)


def _dimension_input(row: Any) -> DimensionRowInput:
    return DimensionRowInput(
        dimension_value=row.dimension_value,
        clicks=row.clicks,
        impressions=row.impressions,
        ctr=row.ctr,
        position=row.position,
        quality=row.quality,
        rank=row.rank,
    )


def _analytics_dataset_input(row: Any) -> AnalyticsDatasetRowInput:
    return AnalyticsDatasetRowInput(
        dataset_key=row.dataset_key,
        dimension_key=row.dimension_key,
        dimension_value=row.dimension_value,
        metrics=dict(row.metrics),
        quality=row.quality,
        rank=row.rank,
    )


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
            "client_domain": client.domain,
            "period_kind": job.period_kind,
            "comparison_mode": job.comparison_mode,
            "anchor_date": job.anchor_date,
            "custom_start": job.custom_start,
            "custom_end": job.custom_end,
            "mode": job.mode,
            "job_params": dict(job.params_json or {}),
            "integration_enabled": integration.enabled if integration else False,
            "integration_config": dict(integration.config_json or {}) if integration else {},
            "source_mode": integration.source_mode if integration else None,
        }

    # `tech_audit` (task-25) and `blogs` (task-26) are self-configured: each
    # audits/crawls the client's own site, so neither has a `client_integrations`
    # row to enable or anything per-client to set up. Requiring one would make
    # every client `not_configured` forever, and creating one would put a
    # non-metric provider into the integration lists the dashboard/UI read.
    # Every other provider keeps the original gate exactly.
    is_self_configured = handle.provider in (Provider.tech_audit, Provider.blogs)
    if not is_self_configured and (
        integration is None or not cell_repo_read["integration_enabled"]
    ):
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
        custom_start=cell_repo_read["custom_start"],
        custom_end=cell_repo_read["custom_end"],
    )

    job_mode = str(cell_repo_read["mode"])
    integration_config = dict(cell_repo_read["integration_config"])
    if is_self_configured:
        # The audit/crawl target is the client's own site; there is no integration
        # row to carry it (see the gate above), so it comes straight off the client.
        integration_config.setdefault("domain", cell_repo_read["client_domain"])

    context = CollectionContext(
        client_id=handle.client_id,
        provider=handle.provider,
        # Dashboard GSC is Reporting Ninja-only. Even an old integration row
        # marked auto/browser is normalized to API at the worker boundary so
        # new snapshots can never reintroduce browser/imported GSC data.
        source_mode=(
            SourceMode.api
            if handle.provider == Provider.gsc
            else cell_repo_read["source_mode"] or SourceMode.auto
        ),
        period_kind=cell_repo_read["period_kind"],
        comparison_mode=cell_repo_read["comparison_mode"],
        current=periods.current,
        previous=periods.previous,
        timezone=cell_repo_read["timezone"],
        # A tech audit / blog crawl is an explicit, on-demand action: "Run audit"
        # / "Refresh data" must always re-crawl, never hit the freshness-skip
        # that exists for routine syncs.
        force_refresh=job_mode in ("force_refresh", "tech_audit", "tech_audit_recheck", "blogs"),
        job_mode=job_mode,
        job_params=cell_repo_read["job_params"],
        integration_config=integration_config,
        browser_slot=handle.slot,
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
            latest = SnapshotRepository(session).latest_success(
                handle.client_id,
                handle.provider,
                source_mode=SourceMode.api if handle.provider == Provider.gsc else None,
            )
        needs_dimension_backfill = (
            handle.provider == Provider.gsc
            and hasattr(adapter, "collect_dimensions")
            and latest is not None
            and not _includes_gsc_dimensions(latest)
        )
        needs_dataset_backfill = (
            hasattr(adapter, "collect_datasets")
            and latest is not None
            and not _includes_analytics_datasets(latest)
        )
        if (
            latest is not None
            and _is_fresh_enough(latest, periods.current.end)
            and not needs_dimension_backfill
            and not needs_dataset_backfill
        ):
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

    dimension_result = None
    collect_dimensions = getattr(adapter, "collect_dimensions", None)
    if handle.provider == Provider.gsc and callable(collect_dimensions):
        dimension_result = _call_with_timeout(
            lambda: collect_dimensions(context, emit), cell_timeout_seconds
        )
        if dimension_result is _TIMEOUT:
            dimension_result = None
            logger.warning("gsc_dimensions_timeout cell_id=%s", handle.cell_id)
        elif dimension_result is _CANCELLED:
            return _terminate(session_scope, handle, CellStatus.cancelled, error_code=None)

    dataset_result = None
    collect_datasets = getattr(adapter, "collect_datasets", None)
    if callable(collect_datasets):
        dataset_result = _call_with_timeout(
            lambda: collect_datasets(context, emit), cell_timeout_seconds
        )
        if dataset_result is _TIMEOUT:
            dataset_result = None
            logger.warning("analytics_datasets_timeout cell_id=%s", handle.cell_id)

    metrics = normalize_result(result)
    validation_warnings = _validate_metrics(metrics)
    snapshot_quality = result.quality
    snapshot_warnings = [*result.warnings, *validation_warnings]
    snapshot_provenance = dict(result.provenance)
    snapshot_data_through = result.data_through
    if dimension_result is not None:
        snapshot_quality = _combine_gsc_dimension_quality(result.quality, dimension_result)
        snapshot_warnings.extend(dimension_result.warnings)
        snapshot_provenance["dimensions"] = {
            "attempted": True,
            "quality": dimension_result.quality.value,
            "row_count": len(dimension_result.rows),
            **dict(dimension_result.provenance),
        }
        if dimension_result.data_through is not None:
            snapshot_data_through = (
                dimension_result.data_through
                if snapshot_data_through is None
                else min(snapshot_data_through, dimension_result.data_through)
            )
    if dataset_result is not None:
        snapshot_quality = _combine_quality(snapshot_quality, dataset_result.quality)
        snapshot_warnings.extend(dataset_result.warnings)
        snapshot_provenance["analytics_datasets"] = {
            "attempted": True,
            "quality": dataset_result.quality.value,
            "row_count": len(dataset_result.rows),
            **dict(dataset_result.provenance),
        }
    # A live `collect()` discovering a dead session mid-batch (e.g. Semrush's
    # `SessionExpired` -> `error_code="auth_required"`) parks the cell the
    # same way the cheap pre-check above does, instead of collapsing into
    # `partial` and losing the signal (PLAN §12 — this was defect #1).
    final_status = (
        CellStatus.awaiting_auth
        if result.error_code == "auth_required"
        else _map_cell_status(snapshot_quality)
    )

    with session_scope() as session:
        snapshot = SnapshotRepository(session).create_immutable(
            job_id=handle.job_id,
            client_id=handle.client_id,
            provider=handle.provider,
            source_mode=context.source_mode,
            observed_at=result.observed_at,
            quality=snapshot_quality,
            data_through=snapshot_data_through,
            warnings=snapshot_warnings,
            provenance=snapshot_provenance,
        )
        MetricRepository(session).bulk_insert(
            snapshot_id=snapshot.id,
            client_id=handle.client_id,
            provider=handle.provider,
            values=metrics,
        )
        if dimension_result is not None:
            by_role: dict[PeriodRole, list[Any]] = {
                PeriodRole.current: [],
                PeriodRole.previous: [],
            }
            for row in dimension_result.rows:
                by_role.setdefault(row.period_role, []).append(row)

            role_snapshots = {PeriodRole.current: snapshot}
            if by_role.get(PeriodRole.previous):
                role_snapshots[PeriodRole.previous] = SnapshotRepository(
                    session
                ).create_immutable(
                    job_id=handle.job_id,
                    client_id=handle.client_id,
                    provider=handle.provider,
                    source_mode=context.source_mode,
                    # Keep the aggregate/current snapshot unambiguously latest.
                    observed_at=result.observed_at - timedelta(microseconds=1),
                    quality=dimension_result.quality,
                    data_through=dimension_result.data_through,
                    warnings=dimension_result.warnings,
                    provenance={
                        "dataset": "gsc_dimensions",
                        "period_role": PeriodRole.previous.value,
                        **dict(dimension_result.provenance),
                    },
                )

            dimension_repo = DimensionRepository(session)
            for role, rows in by_role.items():
                if not rows:
                    continue
                role_snapshot = role_snapshots[role]
                for dimension in ("page", "query"):
                    matching = [row for row in rows if row.dimension == dimension]
                    if not matching:
                        continue
                    first = matching[0]
                    dimension_repo.bulk_insert(
                        snapshot_id=role_snapshot.id,
                        client_id=handle.client_id,
                        dimension=dimension,
                        period_start=first.period_start,
                        period_end=first.period_end,
                        rows=[_dimension_input(row) for row in matching],
                    )
        if dataset_result is not None:
            dataset_by_role: dict[PeriodRole, list[Any]] = {
                PeriodRole.current: [],
                PeriodRole.previous: [],
            }
            for row in dataset_result.rows:
                dataset_by_role.setdefault(row.period_role, []).append(row)

            dataset_snapshots = {PeriodRole.current: snapshot}
            if dataset_by_role.get(PeriodRole.previous):
                dataset_snapshots[PeriodRole.previous] = SnapshotRepository(
                    session
                ).create_immutable(
                    job_id=handle.job_id,
                    client_id=handle.client_id,
                    provider=handle.provider,
                    source_mode=context.source_mode,
                    observed_at=result.observed_at - timedelta(microseconds=1),
                    quality=dataset_result.quality,
                    data_through=result.data_through,
                    warnings=dataset_result.warnings,
                    provenance={
                        "dataset": "analytics_datasets",
                        "period_role": PeriodRole.previous.value,
                        **dict(dataset_result.provenance),
                    },
                )

            dataset_repo = AnalyticsDatasetRepository(session)
            for role, rows in dataset_by_role.items():
                if not rows:
                    continue
                role_snapshot = dataset_snapshots[role]
                for dataset_key in {row.dataset_key for row in rows}:
                    matching = [row for row in rows if row.dataset_key == dataset_key]
                    first = matching[0]
                    dataset_repo.bulk_insert(
                        snapshot_id=role_snapshot.id,
                        client_id=handle.client_id,
                        provider=handle.provider,
                        period_start=first.period_start,
                        period_end=first.period_end,
                        rows=[_analytics_dataset_input(row) for row in matching],
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
    return _call_with_timeout(
        lambda: adapter.collect(context, emit, is_cancel_requested),
        timeout_seconds,
        cancelled_type=CancelledByAdapter,
    )


def _call_with_timeout(
    call: Callable[[], Any],
    timeout_seconds: int,
    *,
    cancelled_type: type[BaseException] | None = None,
) -> Any:
    outcome: dict[str, Any] = {}

    def _run() -> None:
        try:
            outcome["result"] = call()
        except BaseException as exc:  # noqa: BLE001 - relayed to the caller's thread below
            outcome["exc"] = exc

    thread = threading.Thread(target=_run, name="cell-collect", daemon=True)
    thread.start()
    thread.join(timeout=timeout_seconds)
    if thread.is_alive():
        return _TIMEOUT
    if "exc" in outcome:
        exc = outcome["exc"]
        if cancelled_type is not None and isinstance(exc, cancelled_type):
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
