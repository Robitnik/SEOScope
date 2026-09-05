"""URL Inspector screenshot DTOs (task-64 §2) — the shape
`app/api/url_screenshots.py` returns."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

ShotPresetName = Literal["desktop", "tablet", "mobile"]


class UrlScreenshotDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    preset: ShotPresetName
    state: Literal["ok", "failed"]
    width: int | None = None
    height: int | None = None
    fold_height: int | None = None
    byte_size: int | None = None
    truncated: bool = False
    http_status: int | None = None
    final_url: str | None = None
    error: str | None = None
    captured_at: datetime
    # `None` on a `failed` row — never a URL to a file that doesn't exist.
    image_url: str | None = None
    thumb_url: str | None = None


class UrlScreenshotSetDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input_url: str
    inspector_key: str
    # True when this response was read from cache rather than freshly
    # captured just now (task-64 §2.5) — the operator-visible difference
    # between "instant" and "this took ~20s".
    cached: bool
    screenshots: list[UrlScreenshotDTO] = []


class UrlScreenshotRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str
    presets: list[ShotPresetName] | None = None
    force: bool = False
