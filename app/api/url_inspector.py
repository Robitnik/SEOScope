"""URL Inspector endpoints (task-61 + task-88), prefix `/api/v1/url-inspector`.

Calls are split by measured latency, deliberately not merged (task-61 §2.6,
task-88 §2.2) — each panel group renders as soon as its own budget allows,
instead of every panel waiting for the slowest one:

| Endpoint | Source | Budget |
|---|---|---|
| `GET ""` | stored data (six panels) | tens of ms |
| `GET "/google"` | direct Google API — series, page queries, conversions | 0.4-1.6s |
| `GET "/google/breakdowns"` | direct Google API — every dimension slice | ~1-2s |
| `POST "/index-status"` | Search Console URL Inspection API | **~6.6s** |
| `GET "/google/realtime"` | GA4 Realtime | seconds, true only now |
| `POST "/live"` | our own fetch of the page | up to ~20s |
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from sqlalchemy.orm import Session

from app.api import register_router
from app.db.repositories.clients import ClientRepository
from app.db.session import worker_session_scope
from app.schemas.api import ErrorDTO
from app.schemas.url_inspector import (
    Granularity,
    UrlGoogleBreakdownsDTO,
    UrlGoogleCoreDTO,
    UrlIndexStatusDTO,
    UrlIndexStatusRequest,
    UrlInspectorDTO,
    UrlInspectorLiveDTO,
    UrlInspectorLiveRequest,
    UrlRealtimeDTO,
    UrlSuggestResponseDTO,
)
from app.urlinspector.google_service import (
    GoogleTargetUnavailable,
    inspect_url_google_breakdowns,
    inspect_url_google_core,
    inspect_url_index_status,
    inspect_url_realtime,
)
from app.urlinspector.live import run_live_check
from app.urlinspector.resolve import UrlValidationError, parse_input_url, resolve_client_for_url
from app.urlinspector.service import inspect_url
from app.urlinspector.suggest import suggest_urls

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


def _run_suggest(q: str) -> UrlSuggestResponseDTO:
    with worker_session_scope() as session:
        return UrlSuggestResponseDTO(suggestions=suggest_urls(session, q=q))


@router.get("/suggest", response_model=UrlSuggestResponseDTO)
async def get_url_inspector_suggest(q: str = Query("", max_length=2048)) -> UrlSuggestResponseDTO:
    return await run_in_threadpool(_run_suggest, q)


# ── direct Google API panels (task-88) ───────────────────────────────────
#
# `GoogleTargetUnavailable` becomes a **409**, not a 400 or a 500: the request
# is well formed and our code is fine — the client simply has no Google
# integration for this URL yet. The frontend turns that body into the panel's
# own `not_configured` state and keeps every other panel on the page, so an
# unconfigured client never costs the operator the whole `#/url` view.
_GOOGLE_UNAVAILABLE: dict[int | str, dict[str, Any]] = {
    409: {"model": ErrorDTO},
    400: {"model": ErrorDTO},
}


def _google_unavailable(exc: GoogleTargetUnavailable) -> HTTPException:
    return HTTPException(
        status_code=409, detail=_error(exc.state, exc.detail)
    )


@router.get(
    "/google", response_model=UrlGoogleCoreDTO, responses=_GOOGLE_UNAVAILABLE
)
async def get_url_inspector_google(
    url: str = Query(..., max_length=2048), window: str | None = Query(None)
) -> UrlGoogleCoreDTO:
    """Series, window totals, this page's real search queries, conversions by
    name, and the built-in GSC-vs-GA4 cross check."""
    try:
        parsed = parse_input_url(url)
    except UrlValidationError as exc:
        raise HTTPException(status_code=400, detail=_error(exc.code, str(exc))) from exc

    def _run() -> UrlGoogleCoreDTO:
        with worker_session_scope() as session:
            return inspect_url_google_core(session, url=parsed, window_key=window)

    try:
        return await run_in_threadpool(_run)
    except GoogleTargetUnavailable as exc:
        raise _google_unavailable(exc) from exc


@router.get(
    "/google/breakdowns",
    response_model=UrlGoogleBreakdownsDTO,
    responses=_GOOGLE_UNAVAILABLE,
)
async def get_url_inspector_google_breakdowns(
    url: str = Query(..., max_length=2048), window: str | None = Query(None)
) -> UrlGoogleBreakdownsDTO:
    """Every dimension slice of this URL, plus its totals in each search type."""
    try:
        parsed = parse_input_url(url)
    except UrlValidationError as exc:
        raise HTTPException(status_code=400, detail=_error(exc.code, str(exc))) from exc

    def _run() -> UrlGoogleBreakdownsDTO:
        with worker_session_scope() as session:
            return inspect_url_google_breakdowns(session, url=parsed, window_key=window)

    try:
        return await run_in_threadpool(_run)
    except GoogleTargetUnavailable as exc:
        raise _google_unavailable(exc) from exc


@router.get(
    "/google/realtime", response_model=UrlRealtimeDTO, responses=_GOOGLE_UNAVAILABLE
)
async def get_url_inspector_realtime(
    url: str = Query(..., max_length=2048),
) -> UrlRealtimeDTO:
    """Active users right now. Property-wide, never page-scoped — GA4 Realtime
    takes no page filter, and the DTO says so in `scope`."""
    try:
        parsed = parse_input_url(url)
    except UrlValidationError as exc:
        raise HTTPException(status_code=400, detail=_error(exc.code, str(exc))) from exc

    def _run() -> UrlRealtimeDTO:
        with worker_session_scope() as session:
            return inspect_url_realtime(session, url=parsed)

    try:
        return await run_in_threadpool(_run)
    except GoogleTargetUnavailable as exc:
        raise _google_unavailable(exc) from exc


@router.post(
    "/index-status", response_model=UrlIndexStatusDTO, responses=_GOOGLE_UNAVAILABLE
)
async def post_url_inspector_index_status(
    payload: UrlIndexStatusRequest,
) -> UrlIndexStatusDTO:
    """Search Console's own answer to "is this page indexed": coverage state,
    last crawl, robots.txt state, Google's chosen canonical, rich results.

    POST rather than GET, and behind an explicit button in the UI: one call is
    ~6.6s measured, and Google's own quota for it is per-day, not per-minute.
    """
    try:
        parsed = parse_input_url(payload.url)
    except UrlValidationError as exc:
        raise HTTPException(status_code=400, detail=_error(exc.code, str(exc))) from exc

    def _run() -> UrlIndexStatusDTO:
        with worker_session_scope() as session:
            return inspect_url_index_status(session, url=parsed)

    try:
        return await run_in_threadpool(_run)
    except GoogleTargetUnavailable as exc:
        raise _google_unavailable(exc) from exc


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
