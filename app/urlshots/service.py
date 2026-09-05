"""Orchestrator for the URL Inspector's screenshot endpoints (task-64 §2.5)
— capture, store, persist, and read back, all in one place so
`app/api/url_screenshots.py` stays a thin HTTP shell.

Two calls, deliberately not one — same reasoning as task-61's
`/url-inspector` vs. `/url-inspector/live` split (task-61 §2.6): reading a
cached set must be instant, capturing a fresh one is a slow (~20s),
opt-in network round trip that would otherwise block the fast path.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models import UrlScreenshot
from app.db.repositories.clients import ClientRepository
from app.db.repositories.url_screenshots import (
    UrlScreenshotInput,
    UrlScreenshotRepository,
    is_fresh,
)
from app.schemas.url_screenshots import UrlScreenshotDTO, UrlScreenshotSetDTO
from app.urlinspector.resolve import resolve_client_for_url
from app.urlinspector.urlkey import inspector_key
from app.urlshots.capture import PRESET_NAMES, capture_screenshots
from app.urlshots.storage import store_shot

# A capture is expensive (~20s) but a page's above-the-fold look rarely
# changes hour to hour — 24h keeps "open the page twice in a session" free
# without letting the cache go stale for days (task-64 §2.5).
_CACHE_MAX_AGE_SECONDS = 24 * 60 * 60


def _row_to_dto(row: UrlScreenshot) -> UrlScreenshotDTO:
    return UrlScreenshotDTO(
        id=row.id,
        preset=row.preset,  # type: ignore[arg-type]
        state=row.state,  # type: ignore[arg-type]
        width=row.width,
        height=row.height,
        fold_height=row.fold_height,
        byte_size=row.byte_size,
        truncated=row.truncated,
        http_status=row.http_status,
        final_url=row.final_url,
        error=row.error,
        captured_at=row.captured_at,
        image_url=(
            f"/api/v1/url-screenshots/{row.id}/image" if row.blob_sha256 is not None else None
        ),
        thumb_url=(
            f"/api/v1/url-screenshots/{row.id}/thumb" if row.thumb_sha256 is not None else None
        ),
    )


def get_cached_screenshots(session: Session, *, url: str) -> UrlScreenshotSetDTO:
    """Cache-only read — never captures. Used both by the dedicated `GET`
    endpoint and by `app.urlinspector.sources.screenshots`'s coverage check.
    """
    key = inspector_key(url)
    rows = UrlScreenshotRepository(session).latest_set(key, presets=PRESET_NAMES)
    return UrlScreenshotSetDTO(
        input_url=url,
        inspector_key=key,
        cached=True,
        screenshots=[_row_to_dto(r) for r in rows],
    )


def get_or_capture_screenshots(
    session: Session, *, url: str, presets: tuple[str, ...] = PRESET_NAMES, force: bool = False
) -> UrlScreenshotSetDTO:
    key = inspector_key(url)
    repo = UrlScreenshotRepository(session)
    now = datetime.now(UTC)

    existing = repo.latest_set(key, presets=presets)
    if not force and is_fresh(
        existing, presets=presets, now=now, max_age_seconds=_CACHE_MAX_AGE_SECONDS
    ):
        return UrlScreenshotSetDTO(
            input_url=url,
            inspector_key=key,
            cached=True,
            screenshots=[_row_to_dto(r) for r in existing],
        )

    client = resolve_client_for_url(url, ClientRepository(session).list_all()).client
    settings = get_settings()

    captured_rows: list[UrlScreenshot] = []
    for shot in capture_screenshots(url, presets=presets):
        blob_sha = thumb_sha = None
        if shot.state == "ok" and shot.image is not None:
            blob_sha = store_shot(settings.artifacts_dir, shot.image).sha256
            if shot.thumb is not None:
                thumb_sha = store_shot(settings.artifacts_dir, shot.thumb).sha256
        row = repo.create(
            UrlScreenshotInput(
                client_id=client.id if client else None,
                inspector_key=key,
                url=url,
                preset=shot.preset,
                state=shot.state,
                blob_sha256=blob_sha,
                thumb_sha256=thumb_sha,
                width=shot.width,
                height=shot.height,
                fold_height=shot.fold_height,
                byte_size=shot.byte_size,
                truncated=shot.truncated,
                http_status=shot.http_status,
                final_url=shot.final_url,
                error=shot.error,
            )
        )
        captured_rows.append(row)
    session.commit()

    return UrlScreenshotSetDTO(
        input_url=url,
        inspector_key=key,
        cached=False,
        screenshots=[_row_to_dto(r) for r in captured_rows],
    )
