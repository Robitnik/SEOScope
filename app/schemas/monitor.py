"""Site Monitor REST DTOs (task-35 §3.8).

Kept in their own module rather than folded into `app/schemas/api.py`:
the plan's owned-paths table gives this task `app/schemas/monitor.py`
specifically, and the monitor's shapes (runs/changes/versions/diffs) share
nothing structurally with the metrics-provider contract that file already
carries.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Severity = Literal["critical", "warning", "info"]
ChangeScope = Literal["site", "page", "dns", "robots", "sitemap"]
RunKind = Literal["light", "full"]
ChangeState = Literal["open", "all"]


class MonitorSettingsDTO(BaseModel):
    client_id: str
    enabled: bool
    base_url_override: str | None = None
    light_interval_hours: float | None = None
    full_interval_hours: float | None = None
    max_urls: int | None = None
    monitor_when_paused: bool
    ignore_patterns: list[str] = []
    muted_change_types: list[str] = []
    updated_at: datetime


class MonitorSettingsUpdateRequest(BaseModel):
    """`PATCH /clients/{id}/monitor/settings` — every field optional, only
    what's sent is changed (mirrors `ClientUpdateRequest`)."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool | None = None
    base_url_override: str | None = None
    light_interval_hours: float | None = None
    full_interval_hours: float | None = None
    max_urls: int | None = None
    monitor_when_paused: bool | None = None
    ignore_patterns: list[str] | None = None
    muted_change_types: list[str] | None = None


class MonitorRunDTO(BaseModel):
    id: str
    client_id: str
    kind: RunKind
    trigger: Literal["scheduled", "manual"]
    status: str
    started_at: datetime
    finished_at: datetime | None = None
    # `None` for runs started before this field existed — never `0`, so the
    # UI can tell "no total known yet" apart from "target list was empty".
    urls_total: int | None = None
    urls_checked: int
    urls_changed: int
    urls_new: int
    urls_gone: int
    requests_made: int
    bytes_downloaded: int
    changes_critical: int
    changes_warning: int
    changes_info: int
    truncated: bool
    remaining_count: int | None = None
    error_code: str | None = None
    error_message: str | None = None


class MonitorRunAcceptedDTO(BaseModel):
    """`POST /clients/{id}/monitor/runs` — 202, progress reads via polling
    `GET /clients/{id}/monitor/runs` (mirrors the tech-audit "Check now"
    pattern, task-29)."""

    run_id: str
    status_url: str


class MonitorChangeDTO(BaseModel):
    id: str
    client_id: str
    run_id: str | None = None
    severity: Severity
    change_type: str
    scope: ChangeScope
    subject: str
    old_value: str | None = None
    new_value: str | None = None
    details: dict[str, Any] = {}
    detected_at: datetime
    acknowledged_at: datetime | None = None
    acknowledged_note: str | None = None
    resolved_at: datetime | None = None


class MonitorChangeAckRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    note: str | None = Field(default=None, max_length=500)


class SitemapHealthDTO(BaseModel):
    total: int
    ok: int
    redirects: int
    broken: int
    unavailable: int


class MonitorSummaryDTO(BaseModel):
    """`GET /clients/{id}/monitor` — always returns a meaningful shape, even
    for a client with zero runs yet (task-35 DoD): counts are all `0`,
    `last_light_run`/`last_full_run` are `None`, never a 404/500.
    """

    client_id: str
    enabled: bool
    last_light_run: MonitorRunDTO | None = None
    last_full_run: MonitorRunDTO | None = None
    open_critical: int
    open_warning: int
    open_info: int
    sitemap_health: SitemapHealthDTO
    robots_status: int | None = None
    next_light_due_at: datetime | None = None
    next_full_due_at: datetime | None = None


class MonitorResourceDTO(BaseModel):
    id: str
    kind: Literal["robots", "sitemap"]
    url: str
    status: int | None = None
    version_seq: int
    missing_since: datetime | None = None
    last_checked_at: datetime | None = None
    # Hash of the stored file — the handle `GET /monitor/documents/{sha256}`
    # takes, so the UI can show what was actually fetched instead of asking the
    # operator to trust a summary. `None` while nothing has been stored yet.
    content_sha256: str | None = None
    # `urlset` entry count / `index` child count, lifted out of `structure_json`
    # so a file list can be rendered without shipping every URL.
    entry_count: int | None = None
    # "urlset" | "index" | "unknown" — an index file has children, not URLs.
    structure_kind: str | None = None


class MonitorResourceVersionDTO(BaseModel):
    id: str
    observed_at: datetime
    http_status: int | None = None
    content_sha256: str | None = None
    changed_fields: list[str] = []
    prev_version_id: str | None = None


class MonitorResourceDiffDTO(BaseModel):
    from_version_id: str
    to_version_id: str
    changed_fields: list[str]
    from_structure: dict[str, Any]
    to_structure: dict[str, Any]


class MonitorPageDTO(BaseModel):
    id: str
    url: str
    is_key_page: bool
    in_sitemap: bool
    status: int | None = None
    final_url: str | None = None
    indexable: bool | None = None
    # The directive itself, not just the boolean it collapses to: `noindex,
    # follow` and `noindex, nofollow` are different instructions to Google, and
    # `indexable=False` alone cannot tell them apart — nor say whether the
    # directive came from the meta tag or the `X-Robots-Tag` header.
    meta_robots: str | None = None
    x_robots_tag: str | None = None
    title: str | None = None
    canonical: str | None = None
    version_seq: int
    gone_since: datetime | None = None
    consecutive_failures: int
    last_checked_at: datetime | None = None


class MonitorPagesPageDTO(BaseModel):
    pages: list[MonitorPageDTO]
    total: int
    limit: int
    offset: int


class MonitorPageVersionDTO(BaseModel):
    id: str
    observed_at: datetime
    http_status: int | None = None
    signals_sha256: str | None = None
    text_sha256: str | None = None
    raw_sha256: str | None = None
    body_blob_sha256: str | None = None
    changed_fields: list[str] = []
    prev_version_id: str | None = None


class MonitorPageDiffDTO(BaseModel):
    from_version_id: str
    to_version_id: str
    changed_fields: list[str]
    from_signals: dict[str, Any]
    to_signals: dict[str, Any]
    # Unified diff lines (stdlib `difflib`) — a human-readable text diff of
    # the two versions' normalized visible text, when a body was stored for
    # both (task-35 DoD "Діф... читається людиною").
    text_diff: list[str] = []


class MonitorDnsRecordDTO(BaseModel):
    id: str
    name: str
    record_type: str
    values: list[str]
    ttl: int | None = None
    resolver: str | None = None
    version_seq: int
    last_checked_at: datetime | None = None


class MonitorDnsVersionDTO(BaseModel):
    id: str
    observed_at: datetime
    values: list[str]
    ttl: int | None = None
    resolver: str | None = None
    prev_version_id: str | None = None
