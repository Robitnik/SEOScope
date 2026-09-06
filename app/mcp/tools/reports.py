"""Група D+F — Звіти й перформанс (план §5): «дай цифри за період».

`get_client_monthly_performance` — головна відповідь на «як минулий
місяць»; `get_report_data`/`run_client_report` замикають патерн
`run_* → job_id → get_job_status → get_report_data` (план §6) для звітів.

Типи `ReportKind`/`ReportSource`/`YearlySpan` беруться з
`app/schemas/reports.py` (чистий Pydantic-модуль, без доменних
залежностей) — той самий контракт, що й REST API, без ризику розходження.
"""

from __future__ import annotations

from typing import Any

from app.mcp.apiclient import get_api_client
from app.mcp.models import (
    MonthlyPerformance,
    MonthlyRow,
    ReportData,
    ReportRunAccepted,
    ReportTableRow,
)
from app.mcp.resolve import resolve_client
from app.mcp.server import mcp
from app.schemas.reports import ReportKind, ReportSource, YearlySpan


@mcp.tool()
async def get_client_monthly_performance(
    client: str,
    months: int = 6,
) -> MonthlyPerformance:
    """Get a client's monthly performance table: organic traffic,
    conversions, and GSC clicks/impressions per month, with each metric's
    direction already resolved (up/down/flat — semantically, not just by
    the raw sign of the change).

    Args:
        client: The client's name or domain.
        months: How many of the most recent months to return.
    """
    api = get_api_client()
    resolved = await resolve_client(api, client)
    table = await api.get(f"/api/v1/clients/{resolved.client_id}/monthly-table")

    rows = sorted(table.get("rows", []), key=lambda r: r["month"])[-months:]
    return MonthlyPerformance(
        client_name=resolved.name,
        collected_at=table.get("collected_at"),
        rows=[
            MonthlyRow(
                month=r["month"],
                label=r["label"],
                organic_users=r.get("organic_users"),
                organic_conversions=r.get("organic_conversions"),
                key_event_count=r.get("key_event_count"),
                gsc_clicks=r.get("gsc_clicks"),
                gsc_impressions=r.get("gsc_impressions"),
                is_partial_period=r.get("is_partial_period", False),
                directions=r.get("directions", {}),
            )
            for r in rows
        ],
    )


@mcp.tool()
async def get_report_data(run_id: str) -> ReportData:
    """Get the finished data table for a report run — the actual numbers,
    not a PDF. Use `run_client_report` to create a run if you don't already
    have a `run_id`.

    Args:
        run_id: A report run id (from `run_client_report`'s result).
    """
    api = get_api_client()
    table = await api.get(f"/api/v1/reports/runs/{run_id}/table")
    periods = [p["label"] for p in table.get("periods", [])]
    rows = [
        ReportTableRow(
            metric_key=r["key"],
            label=r["label"],
            values=r.get("values", []),
            changes=r.get("changes", []),
        )
        for r in table.get("rows", [])
    ]
    return ReportData(
        run_id=table["run_id"],
        kind=table["kind"],
        source=table["source"],
        periods=periods,
        rows=rows,
    )


@mcp.tool()
async def run_client_report(
    client: str,
    kind: ReportKind,
    years: list[int] | None = None,
    months: list[str] | None = None,
    span: YearlySpan | None = None,
    source: ReportSource = "api",
    force_refresh: bool = False,
) -> ReportRunAccepted:
    """Start collecting a report for one client. Runs in the background —
    poll the result with `get_job_status` (kind="report_run"), then read
    the numbers with `get_report_data` once it's done.

    Args:
        client: The client's name or domain.
        kind: "monthly" (calendar months), "yearly" (full/YTD years),
            "custom_months" (an explicit list of months), or "comparator".
        years: Required for kind="yearly", e.g. [2025, 2026].
        months: Required for kind="custom_months", e.g. ["2026-06",
            "2026-07"].
        span: For kind="yearly": "ytd" or "full".
        source: "api" (live data, default) or "pdf" (parsed from an
            uploaded Reporting Ninja PDF).
        force_refresh: Re-collect periods that were already fetched,
            instead of reusing cached data.
    """
    api = get_api_client()
    resolved = await resolve_client(api, client)
    body: dict[str, Any] = {
        "client_id": resolved.client_id,
        "kind": kind,
        "source": source,
        "years": years,
        "months": months,
        "span": span,
        "force_refresh": force_refresh,
    }
    result = await api.post("/api/v1/reports/runs", json=body)
    return ReportRunAccepted(run_id=result["run_id"])
