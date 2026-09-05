"""Page Inventory REST API (task-57), prefix `/api/v1/clients/{client_id}/pages`.

A NEW router module — self-registering via `app.api.register_router`, same
as `app/api/client_monthly.py` (task-54); `app/main.py` needs no edit.

Deliberately synchronous, like task-54's own reasoning: a rebuild reads
already-collected monitor data and runs the QA engine in-process — measured
at ~2.6s for a 443-page client (task-57 §1.7) — a queue/SSE round trip
would cost more than it buys. The actual site re-crawl ("Re-crawl site" in
the UI) is NOT this endpoint — it's the existing `POST .../monitor/runs`
(task-36), orchestrated entirely from the frontend (task-57 §2.2a); this
router never touches `app/monitor/**`.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.api import register_router
from app.core.config import get_settings
from app.db.repositories.clients import ClientRepository
from app.db.session import worker_session_scope
from app.pageinventory import service
from app.pageinventory.xlsx import build_page_inventory_xlsx
from app.schemas.api import ErrorDTO
from app.schemas.page_inventory import (
    PageInventoryRebuildResponseDTO,
    PageInventorySummaryDTO,
    PageInventoryTableDTO,
)

router = APIRouter(prefix="/api/v1/clients/{client_id}/pages", tags=["page-inventory"])

_NOT_FOUND = ErrorDTO(error_code="client_not_found", message="Client not found").model_dump()
_NEVER_BUILT = ErrorDTO(
    error_code="never_built",
    message="No Page Inventory has been built for this client yet — use Rebuild.",
).model_dump()
_MONITOR_DISABLED = ErrorDTO(
    error_code="monitor_disabled",
    message="Site Monitor is off for this client — nothing to build the inventory from.",
).model_dump()
_NO_PAGES_YET = ErrorDTO(
    error_code="no_pages_yet",
    message="Site Monitor has not crawled this client yet.",
).model_dump()


def _client_exists(session: Session, client_id: str) -> bool:
    return ClientRepository(session).get(client_id) is not None


def _get_table(
    client_id: str, *, profile: Literal["calibrated", "strict"], q: str | None,
    status: str | None, field: str | None, severity: str | None, page: int, page_size: int,
) -> PageInventoryTableDTO | str:
    with worker_session_scope() as session:
        if not _client_exists(session, client_id):
            return "client_not_found"
        table = service.get_table(
            session, client_id, profile=profile, query=q, status=status,
            field=field, severity=severity, page=page, page_size=page_size,
        )
        if table is None:
            return "never_built"
        return table


@router.get("", responses={404: {"model": ErrorDTO}})
async def get_page_inventory(
    client_id: str,
    profile: Literal["calibrated", "strict"] = "calibrated",
    q: str | None = None,
    status: str | None = Query(default=None),
    field: str | None = None,
    severity: str | None = None,
    page: int = 1,
    page_size: int = 50,
) -> PageInventoryTableDTO:
    result = await run_in_threadpool(
        _get_table, client_id, profile=profile, q=q, status=status,
        field=field, severity=severity, page=page, page_size=page_size,
    )
    if result == "client_not_found":
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    if result == "never_built":
        raise HTTPException(status_code=404, detail=_NEVER_BUILT)
    assert isinstance(result, PageInventoryTableDTO)
    return result


def _rebuild(client_id: str) -> PageInventoryTableDTO | str:
    settings = get_settings()
    with worker_session_scope() as session:
        if not _client_exists(session, client_id):
            return "client_not_found"
        outcome = service.rebuild(session, client_id, settings)
        if outcome == "monitor_disabled":
            return "monitor_disabled"
        if outcome == "no_pages_yet":
            return "no_pages_yet"
        table = service.get_table(session, client_id)
        assert table is not None
        return table


@router.post(
    "/rebuild",
    responses={404: {"model": ErrorDTO}, 409: {"model": ErrorDTO}},
)
async def rebuild_page_inventory(client_id: str) -> PageInventoryRebuildResponseDTO:
    result = await run_in_threadpool(_rebuild, client_id)
    if result == "client_not_found":
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    if result == "monitor_disabled":
        raise HTTPException(status_code=409, detail=_MONITOR_DISABLED)
    if result == "no_pages_yet":
        raise HTTPException(status_code=409, detail=_NO_PAGES_YET)
    assert isinstance(result, PageInventoryTableDTO)
    return PageInventoryRebuildResponseDTO(table=result)


@router.get("/summary", responses={404: {"model": ErrorDTO}})
async def get_page_inventory_summary(client_id: str) -> PageInventorySummaryDTO:
    result = await run_in_threadpool(
        _get_table, client_id, profile="calibrated", q=None, status=None,
        field=None, severity=None, page=1, page_size=1,
    )
    if result == "client_not_found":
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    if result == "never_built":
        raise HTTPException(status_code=404, detail=_NEVER_BUILT)
    assert isinstance(result, PageInventoryTableDTO)
    return result.summary


def _build_xlsx(client_id: str) -> tuple[str, bytes] | str:
    with worker_session_scope() as session:
        client = ClientRepository(session).get(client_id)
        if client is None:
            return "client_not_found"
        table = service.get_table(session, client_id, profile="strict", page=1, page_size=100_000)
        if table is None:
            return "never_built"
        client_name = client.name
        rows = table.rows
    data = build_page_inventory_xlsx(rows)
    safe_name = "".join(c if c.isalnum() else "_" for c in client_name).strip("_") or client_id
    filename = f"{safe_name}_Page_Inventory.xlsx"
    return filename, data


@router.get("/xlsx", responses={404: {"model": ErrorDTO}})
async def export_page_inventory_xlsx(client_id: str) -> StreamingResponse:
    result = await run_in_threadpool(_build_xlsx, client_id)
    if result == "client_not_found":
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    if result == "never_built":
        raise HTTPException(status_code=404, detail=_NEVER_BUILT)
    assert isinstance(result, tuple)
    filename, data = result
    return StreamingResponse(
        iter([data]),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


register_router(router)
