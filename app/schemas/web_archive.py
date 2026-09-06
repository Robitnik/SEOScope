"""DTOs for the Web Archive (task-59), prefix `/api/v1/archive`.

Same conventions as `app/schemas/page_inventory.py`: `Literal[...]` aliases
instead of `enum.Enum`, read DTOs left open (no `extra="forbid"`). All UI
copy that surfaces these `Literal` values verbatim is English (CLAUDE.md).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel

ArchiveUrlSource = Literal["sitemap", "link", "manual"]
CaptureTrigger = Literal["scheduled", "manual"]
CaptureStatus = Literal["queued", "running", "ok", "partial", "failed", "blocked", "cancelled"]
CaptureScope = Literal["client", "url"]
CaptureEventLevel = Literal["debug", "info", "warning", "error"]
StoreReason = Literal["first", "changed", "weekly_refresh", "status_changed"]
ChangeKind = Literal[
    "title", "description", "h1", "text", "links", "images", "status", "redirect", "noindex"
]


class ArchiveUrlDTO(BaseModel):
    id: str
    client_id: str
    client_name: str
    url: str
    source: ArchiveUrlSource
    first_captured_at: datetime | None
    last_captured_at: datetime | None
    snapshot_count: int
    gone_since: datetime | None


class ArchiveSnapshotSummaryDTO(BaseModel):
    id: str
    ts: str  # 14-digit replay timestamp for `observed_at`
    observed_at: datetime
    last_confirmed_at: datetime
    confirmed_count: int
    http_status: int | None
    store_reason: StoreReason
    title: str | None
    asset_count: int
    assets_truncated: bool
    has_change: bool  # an `ArchiveChange` row exists against the PREVIOUS snapshot


class ArchiveUrlTimelineDTO(BaseModel):
    url: ArchiveUrlDTO
    snapshots: list[ArchiveSnapshotSummaryDTO]  # newest first


class ArchiveSnapshotAssetDTO(BaseModel):
    url: str
    kind: str
    status: Literal["captured", "missing"]
    content_type: str | None


class ArchiveSnapshotDetailDTO(BaseModel):
    id: str
    archive_url_id: str
    client_id: str
    client_name: str
    url: str
    ts: str
    view_url: str  # `/archive/r/<ts>/<original url>`
    observed_at: datetime
    last_confirmed_at: datetime
    confirmed_count: int
    http_status: int | None
    final_url: str | None
    content_type: str | None
    title: str | None
    description: str | None
    canonical: str | None
    meta_robots: str | None
    word_count: int | None
    headers: dict[str, str]
    asset_count: int
    assets_truncated: bool
    assets_missing: int
    live_iframe_hosts: list[str]  # task-60 §2.3 — hostnames loaded live, not archived
    store_reason: StoreReason
    prev_snapshot_id: str | None
    next_snapshot_id: str | None
    # task-63 §3.5 — count of `archive_replay_misses` rows for this
    # snapshot's OWN HOST (misses aren't linked to a specific snapshot; no
    # migration adds that link here), so an operator staring at
    # "0 missing" assets still sees that the page tried to load something
    # the archive never captured at all.
    replay_misses_for_host: int


class ArchiveChangeDTO(BaseModel):
    id: str
    client_id: str
    client_name: str
    archive_url_id: str
    url: str
    snapshot_id: str
    prev_snapshot_id: str | None
    detected_at: datetime
    change_kinds: list[ChangeKind]
    summary: dict[str, Any]
    text_delta_words: int


class ArchiveChangesPageDTO(BaseModel):
    items: list[ArchiveChangeDTO]
    total: int
    page: int
    page_size: int


class ArchiveChangeDiffDTO(BaseModel):
    change: ArchiveChangeDTO
    diff_lines: list[str]  # unified-diff style, already prefixed +/-/space


class ArchiveCaptureDTO(BaseModel):
    id: str
    client_id: str
    client_name: str
    slot_key: str
    scheduled_for: datetime
    trigger: CaptureTrigger
    status: CaptureStatus
    started_at: datetime | None
    finished_at: datetime | None
    urls_total: int | None
    urls_captured: int
    urls_unchanged: int
    urls_new: int
    urls_failed: int
    urls_blocked: int
    assets_fetched: int
    assets_reused: int
    assets_failed: int
    bytes_stored: int
    error_code: str | None
    error_message: str | None
    # task-62: real queue + per-URL progress/log.
    scope: CaptureScope
    target_url: str | None  # the one URL for `scope="url"`; `None` for `scope="client"`
    progress_message: str | None  # the URL last touched, structured (§2.5), not parsed from a log
    cancel_requested: bool
    queue_position: int | None  # 1-based, among `queued` rows only; `None` off that list


class ArchiveCaptureEventDTO(BaseModel):
    seq: int
    level: CaptureEventLevel
    stage: str | None
    message: str
    details: dict[str, Any]
    created_at: datetime


class ArchiveQueueDTO(BaseModel):
    running: list[ArchiveCaptureDTO]
    queued: list[ArchiveCaptureDTO]  # FIFO order, §1.3 — no priority class
    recent: list[ArchiveCaptureDTO]


class ArchiveCaptureAcceptedDTO(BaseModel):
    capture_id: str
    status_url: str


# task-63 follow-up (live-reported 2026-09-05): the hub's own URL lookup on
# a never-before-seen URL returns a bare `found=False` with no client to
# act on — an operator staring at "Not archived (yet)" had no path forward
# besides waiting for the next portfolio-wide discovery pass. This is the
# accepted-response shape for capturing that URL on the spot; carries the
# client the URL's host resolved to so the UI can show "capturing on
# <CLIENT>" instead of a bare capture id.
class ArchiveAdhocCaptureAcceptedDTO(BaseModel):
    capture_id: str
    status_url: str
    url_id: str
    client_id: str
    client_name: str


class ArchiveCaptureBatchAcceptedDTO(BaseModel):
    capture_ids: list[str]  # one per client actually enqueued (§1.2 "Capture all")
    skipped_client_ids: list[str]  # already had an active capture — not double-queued


class ArchiveCaptureCancelDTO(BaseModel):
    capture_id: str
    # Not `CaptureStatus`: "cancelled" (a `queued` row, finished right away
    # since it never started) or "cancel_requested" (a `running` row, only
    # flagged — the capture loop stops itself between URLs, task-62 §1.6).
    status: Literal["cancelled", "cancel_requested"]


class ArchiveClientCoverageDTO(BaseModel):
    client_id: str
    client_name: str
    enabled: bool
    urls_tracked: int
    urls_known_to_monitor: int
    snapshots_total: int
    last_capture_at: datetime | None
    last_capture_status: CaptureStatus | None
    next_capture_at: datetime | None


class ArchiveOverviewDTO(BaseModel):
    archive_enabled: bool
    cloud_enabled: bool
    timezone: str
    capture_times: list[str]
    clients: list[ArchiveClientCoverageDTO]
    urls_total: int
    snapshots_total: int
    blobs_total: int
    bytes_local: int
    bytes_uploaded: int
    bytes_not_uploaded: int
    assets_referenced: int  # task-60 §1.4 — every discovered asset, fetch outcome notwithstanding
    assets_captured: int  # the subset with at least one stored version
    top_changing_urls: list[ArchiveUrlDTO]
    recent_captures: list[ArchiveCaptureDTO]


class ArchiveUrlSearchResultDTO(BaseModel):
    id: str
    client_id: str
    client_name: str
    url: str
    snapshot_count: int
    last_captured_at: datetime | None


class ArchiveUrlSearchResponseDTO(BaseModel):
    items: list[ArchiveUrlSearchResultDTO]


class ArchiveLookupDTO(BaseModel):
    found: bool
    url: ArchiveUrlDTO | None = None
    # task-70: filled even when `found` is false, whenever the URL's own
    # host resolves to one of OUR clients (`resolve_client_for_url`) — lets
    # the hub/viewer decide whether "Archive this page now" makes sense
    # BEFORE the operator clicks it, instead of only learning from a 404
    # after the fact. `None`/`False` for a URL that isn't ours at all.
    client_id: str | None = None
    client_name: str | None = None
    capturable: bool = False


class ArchiveSettingsDTO(BaseModel):
    client_id: str
    enabled: bool
    capture_assets: bool
    max_urls: int | None
    include_query_strings: bool
    ignore_patterns: list[str]


class ArchiveSettingsUpdateRequest(BaseModel):
    enabled: bool | None = None
    capture_assets: bool | None = None
    max_urls: int | None = None
    include_query_strings: bool | None = None
    ignore_patterns: list[str] | None = None
