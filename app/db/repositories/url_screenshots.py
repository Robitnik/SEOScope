"""Persistence for the URL Inspector's screenshot panel (`url_screenshots`,
task-64 §2.4).

History is kept, never overwritten — multiple rows per `(inspector_key,
preset)` over time, mirroring `ArchiveSnapshot`'s own "append, don't
update" shape for HTML. Only reads/inserts here; nothing about this table
is mutable after a row is written.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import UrlScreenshot


@dataclass(frozen=True)
class UrlScreenshotInput:
    client_id: str | None
    inspector_key: str
    url: str
    preset: str
    state: str
    blob_sha256: str | None
    thumb_sha256: str | None
    width: int | None
    height: int | None
    fold_height: int | None
    byte_size: int | None
    truncated: bool
    http_status: int | None
    final_url: str | None
    error: str | None


class UrlScreenshotRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def create(self, data: UrlScreenshotInput) -> UrlScreenshot:
        row = UrlScreenshot(
            client_id=data.client_id,
            inspector_key=data.inspector_key,
            url=data.url,
            preset=data.preset,
            state=data.state,
            blob_sha256=data.blob_sha256,
            thumb_sha256=data.thumb_sha256,
            width=data.width,
            height=data.height,
            fold_height=data.fold_height,
            byte_size=data.byte_size,
            truncated=data.truncated,
            http_status=data.http_status,
            final_url=data.final_url,
            error=data.error,
        )
        self._session.add(row)
        self._session.flush()
        return row

    def latest_set(self, inspector_key: str, *, presets: tuple[str, ...]) -> list[UrlScreenshot]:
        """At most one row per requested preset — the most recently captured
        one, whether it succeeded or failed. Missing entirely from the
        result means "never attempted for this preset", never a fabricated
        `failed` row (PLAN §12)."""
        latest: list[UrlScreenshot] = []
        for preset in presets:
            row = self._session.scalars(
                select(UrlScreenshot)
                .where(
                    UrlScreenshot.inspector_key == inspector_key,
                    UrlScreenshot.preset == preset,
                )
                .order_by(UrlScreenshot.captured_at.desc())
                .limit(1)
            ).first()
            if row is not None:
                latest.append(row)
        return latest

    def get(self, screenshot_id: str) -> UrlScreenshot | None:
        return self._session.get(UrlScreenshot, screenshot_id)


def is_fresh(
    rows: list[UrlScreenshot],
    *,
    presets: tuple[str, ...],
    now: datetime,
    max_age_seconds: float,
) -> bool:
    """True only when EVERY requested preset has a row within `max_age_seconds`
    — a set with one missing/stale preset is treated as stale as a whole, so
    a caller re-captures the gap rather than silently showing a partial,
    aging set forever."""
    by_preset = {row.preset: row for row in rows}
    for preset in presets:
        row = by_preset.get(preset)
        if row is None:
            return False
        age = (now - row.captured_at).total_seconds()
        if age > max_age_seconds:
            return False
    return True
