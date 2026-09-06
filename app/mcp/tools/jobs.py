"""Група H — Операційне (план §6): один tool на всі фонові роботи.

Чотири підсистеми (звіти, sync-jobs, архів, монітор) мають власні
ендпоінти статусу, але для моделі це одне поняття: «я щось запустив, чи
готово». Дискримінатор `kind` економить три описи в контексті й прибирає
цілий клас неправильних виборів tool-а (план §6).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from mcp.types import ToolAnnotations

from app.mcp.apiclient import get_api_client
from app.mcp.errors import ApiError
from app.mcp.models import JobStatusReport
from app.mcp.resolve import resolve_client
from app.mcp.server import mcp

JobKind = Literal["report_run", "sync_job", "archive_capture", "monitor_run"]

_TERMINAL: dict[str, frozenset[str]] = {
    "report_run": frozenset({"succeeded", "partial", "failed", "cancelled"}),
    "sync_job": frozenset({"succeeded", "partial", "failed", "cancelled"}),
    "archive_capture": frozenset({"ok", "partial", "failed", "blocked", "cancelled"}),
}
# `MonitorRunDTO.status` is a plain `str`, not a closed enum on our side —
# the inverse check (only these are known NOT-done) is the safer heuristic
# here, so a new terminal value added later doesn't silently read as "still
# running" forever.
_MONITOR_NOT_DONE = frozenset({"queued", "running"})

_READ_WITH: dict[str, str] = {"report_run": "get_report_data"}


@dataclass(frozen=True)
class _RawStatus:
    status: str
    progress_percent: float | None
    error_message: str | None


def _ratio(done: Any, total: Any) -> float | None:
    return 100.0 * done / total if total and done is not None else None


async def _report_run(job_id: str) -> _RawStatus:
    row = await get_api_client().get(f"/api/v1/reports/runs/{job_id}")
    return _RawStatus(
        status=row["status"],
        progress_percent=_ratio(row.get("periods_done"), row.get("period_count")),
        error_message=row.get("error_message"),
    )


async def _sync_job(job_id: str) -> _RawStatus:
    row = await get_api_client().get(f"/api/v1/sync-jobs/{job_id}")
    # `GET /sync-jobs/{id}` returns either a `JobDTO` or a `BatchProgressDTO`
    # (a batch parent id) — `.get()` on the raw dict handles both without
    # caring which shape came back.
    return _RawStatus(
        status=row["status"],
        progress_percent=row.get("progress_percent"),
        error_message=row.get("error_message"),
    )


async def _archive_capture(job_id: str) -> _RawStatus:
    row = await get_api_client().get(f"/api/v1/archive/captures/{job_id}")
    return _RawStatus(
        status=row["status"],
        progress_percent=_ratio(row.get("urls_captured"), row.get("urls_total")),
        error_message=row.get("error_message"),
    )


async def _monitor_run(job_id: str, client: str | None) -> _RawStatus:
    if not client:
        raise ApiError(
            'kind="monitor_run" also needs `client`: monitor runs have no standalone '
            "status endpoint, only a per-client list that this tool searches."
        )
    api = get_api_client()
    resolved = await resolve_client(api, client)
    rows = await api.get(f"/api/v1/clients/{resolved.client_id}/monitor/runs")
    row = next((r for r in rows if r["id"] == job_id), None)
    if row is None:
        raise ApiError(f'No monitor run "{job_id}" found for client "{client}".')
    return _RawStatus(
        status=row["status"],
        progress_percent=_ratio(row.get("urls_checked"), row.get("urls_total")),
        error_message=row.get("error_message"),
    )


def _is_done(kind: str, status: str) -> bool:
    if kind == "monitor_run":
        return status not in _MONITOR_NOT_DONE
    return status in _TERMINAL[kind]


@mcp.tool(
    title="Check job status",
    annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
)
async def get_job_status(
    job_id: str,
    kind: JobKind,
    client: str | None = None,
) -> JobStatusReport:
    """Check on a background job started by another tool: report runs,
    metric collection, archive captures, or monitor runs.

    Args:
        job_id: The id returned by whichever tool started the job (e.g.
            `run_client_report`'s `run_id`).
        kind: What kind of job this is — matches the `kind` field in that
            tool's result.
        client: Required only when kind="monitor_run" (the client's name
            or domain) — monitor runs are looked up per client, not by a
            global id.
    """
    handlers: dict[str, Any] = {
        "report_run": lambda: _report_run(job_id),
        "sync_job": lambda: _sync_job(job_id),
        "archive_capture": lambda: _archive_capture(job_id),
        "monitor_run": lambda: _monitor_run(job_id, client),
    }
    raw = await handlers[kind]()
    done = _is_done(kind, raw.status)
    return JobStatusReport(
        job_id=job_id,
        kind=kind,
        status=raw.status,
        progress_percent=raw.progress_percent,
        error_message=raw.error_message,
        done=done,
        read_result_with=_READ_WITH.get(kind) if done and raw.error_message is None else None,
    )
