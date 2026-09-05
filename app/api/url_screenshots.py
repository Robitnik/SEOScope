"""URL Inspector screenshot endpoints (task-64 §2), prefix
`/api/v1/url-screenshots`.

Two calls, deliberately not one — the same split as
`/url-inspector` vs. `/url-inspector/live` (task-61 §2.6): `GET` reads
whatever is cached and returns instantly; `POST` is the slow (~20s), opt-in
capture. Image bytes are served content-addressed and immutable
(`Cache-Control: immutable`) — a screenshot row is never updated after
creation, so its blob can never change under a given id.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse

from app.api import register_router
from app.core.config import get_settings
from app.db.repositories.url_screenshots import UrlScreenshotRepository
from app.db.session import worker_session_scope
from app.schemas.api import ErrorDTO
from app.schemas.url_screenshots import UrlScreenshotRequest, UrlScreenshotSetDTO
from app.tools.http_safety import UnsafeUrlError
from app.urlinspector.resolve import UrlValidationError, parse_input_url
from app.urlshots.capture import PRESET_NAMES
from app.urlshots.service import get_cached_screenshots, get_or_capture_screenshots
from app.urlshots.storage import resolve_shot_path

router = APIRouter(prefix="/api/v1/url-screenshots", tags=["url-screenshots"])

_NOT_FOUND = {"error_code": "not_found", "message": "Screenshot not found."}
_IMMUTABLE_CACHE_HEADERS = {"Cache-Control": "public, max-age=31536000, immutable"}


def _error(code: str, message: str) -> dict[str, object]:
    return ErrorDTO(error_code=code, message=message).model_dump()


def _run_get_cached(url: str) -> UrlScreenshotSetDTO:
    with worker_session_scope() as session:
        return get_cached_screenshots(session, url=url)


@router.get("", response_model=UrlScreenshotSetDTO, responses={400: {"model": ErrorDTO}})
async def get_url_screenshots(url: str = Query(..., max_length=2048)) -> UrlScreenshotSetDTO:
    try:
        parsed = parse_input_url(url)
    except UrlValidationError as exc:
        raise HTTPException(status_code=400, detail=_error(exc.code, str(exc))) from exc
    return await run_in_threadpool(_run_get_cached, parsed)


def _run_capture(url: str, presets: tuple[str, ...], force: bool) -> UrlScreenshotSetDTO:
    with worker_session_scope() as session:
        return get_or_capture_screenshots(session, url=url, presets=presets, force=force)


@router.post("", response_model=UrlScreenshotSetDTO, responses={400: {"model": ErrorDTO}})
async def post_url_screenshots(payload: UrlScreenshotRequest) -> UrlScreenshotSetDTO:
    try:
        parsed = parse_input_url(payload.url)
    except UrlValidationError as exc:
        raise HTTPException(status_code=400, detail=_error(exc.code, str(exc))) from exc
    presets = tuple(payload.presets) if payload.presets else PRESET_NAMES
    try:
        return await run_in_threadpool(_run_capture, parsed, presets, payload.force)
    except UnsafeUrlError as exc:
        raise HTTPException(status_code=400, detail=_error(exc.code, str(exc))) from exc


def _resolve_image(screenshot_id: str, *, thumb: bool) -> Path | None:
    with worker_session_scope() as session:
        row = UrlScreenshotRepository(session).get(screenshot_id)
        if row is None:
            return None
        sha256 = row.thumb_sha256 if thumb else row.blob_sha256
    if sha256 is None:
        return None
    settings = get_settings()
    try:
        path = resolve_shot_path(settings.artifacts_dir, sha256)
    except PermissionError:
        return None
    return path if path.is_file() else None


@router.get("/{screenshot_id}/image", responses={404: {"model": ErrorDTO}})
async def get_url_screenshot_image(screenshot_id: str) -> FileResponse:
    path = await run_in_threadpool(_resolve_image, screenshot_id, thumb=False)
    if path is None:
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    return FileResponse(path, media_type="image/jpeg", headers=_IMMUTABLE_CACHE_HEADERS)


@router.get("/{screenshot_id}/thumb", responses={404: {"model": ErrorDTO}})
async def get_url_screenshot_thumb(screenshot_id: str) -> FileResponse:
    path = await run_in_threadpool(_resolve_image, screenshot_id, thumb=True)
    if path is None:
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    return FileResponse(path, media_type="image/jpeg", headers=_IMMUTABLE_CACHE_HEADERS)


register_router(router)
