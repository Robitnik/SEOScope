"""Screenshot coverage source for the URL Inspector (task-64 §2, §5).

Cache-only — reads whatever `url_screenshots` rows already exist and NEVER
triggers a capture. A live screenshot run takes ~20s (task-64 §0.1); running
it on every `GET /url-inspector` would cost every other, instant panel that
same latency. Capturing happens only through the dedicated
`POST /api/v1/url-screenshots` endpoint, opt-in exactly like
`POST /url-inspector/live` (task-61 §2.6).
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.db.repositories.url_screenshots import UrlScreenshotRepository
from app.schemas.url_inspector import UrlScreenshotsStateDTO, UrlSourceStateDTO
from app.urlinspector.urlkey import inspector_key
from app.urlshots.capture import PRESET_NAMES


def collect_screenshots(session: Session, *, client_id: str, url: str) -> UrlScreenshotsStateDTO:
    key = inspector_key(url)
    rows = UrlScreenshotRepository(session).latest_set(key, presets=PRESET_NAMES)
    if not rows:
        return UrlScreenshotsStateDTO(
            state=UrlSourceStateDTO(
                source="screenshots",
                state="no_data",
                detail="No screenshots captured for this URL yet.",
            )
        )
    return UrlScreenshotsStateDTO(
        state=UrlSourceStateDTO(source="screenshots", state="has_data"),
        preset_count=len(rows),
    )
