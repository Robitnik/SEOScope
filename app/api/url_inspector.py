"""URL Inspector endpoints (task-61), prefix `/api/v1/url-inspector`.

Two calls, deliberately not one (task-61 §2.6): stored data must render
instantly, the live micro audit is a slow, opt-in network round trip (up to
~20s on an antibot-guarded host) that would otherwise block the five panels
that don't need it.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from sqlalchemy.orm import Session

from app.api import register_router
from app.db.repositories.clients import ClientRepository
from app.db.session import worker_session_scope
from app.schemas.api import ErrorDTO
from app.schemas.url_inspector import (
    Granularity,
    UrlInspectorDTO,
    UrlInspectorLiveDTO,
    UrlInspectorLiveRequest,
)
from app.urlinspector.live import run_live_check
from app.urlinspector.resolve import UrlValidationError, parse_input_url, resolve_client_for_url
from app.urlinspector.service import inspect_url

router = APIRouter(prefix="/api/v1/url-inspector", tags=["url-inspector"])


def _error(code: str, message: str) -> dict[str, object]:
    return ErrorDTO(error_code=code, message=message).model_dump()


def _run_inspect(url: str, granularity: Granularity) -> UrlInspectorDTO:
    with worker_session_scope() as session:
        return inspect_url(session, url=url, granularity=granularity)


def _resolved_client_id(session: Session, url: str) -> str | None:
    resolved = resolve_client_for_url(url, ClientRepository(session).list_all())
    return resolved.client.id if resolved.client else None


@router.get("", response_model=UrlInspectorDTO, responses={400: {"model": ErrorDTO}})
async def get_url_inspector(
    url: str = Query(..., max_length=2048), granularity: Granularity = "week"
) -> UrlInspectorDTO:
    try:
        parsed = parse_input_url(url)
    except UrlValidationError as exc:
        raise HTTPException(status_code=400, detail=_error(exc.code, str(exc))) from exc
    return await run_in_threadpool(_run_inspect, parsed, granularity)


@router.post("/live", response_model=UrlInspectorLiveDTO, responses={400: {"model": ErrorDTO}})
async def post_url_inspector_live(payload: UrlInspectorLiveRequest) -> UrlInspectorLiveDTO:
    try:
        parsed = parse_input_url(payload.url)
    except UrlValidationError as exc:
        raise HTTPException(status_code=400, detail=_error(exc.code, str(exc))) from exc

    def _run() -> UrlInspectorLiveDTO:
        with worker_session_scope() as session:
            client_id = _resolved_client_id(session, parsed)
            return run_live_check(
                session, client_id=client_id, url=parsed, with_schema=payload.with_schema
            )

    return await run_in_threadpool(_run)


register_router(router)
