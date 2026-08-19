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
# "page" (task-38 §2): an operator re-checking one URL from the Pages table,
# not a client-wide sweep — a distinct kind rather than `run_id=None` or
# attaching to the last light/full run, so the run log stays honest about
# what actually produced a change (task-37 F4 already fixed one case of a
# change misattributed to the wrong run; this avoids reintroducing that).
RunKind = Literal["light", "full", "page"]
ChangeState = Literal["open", "all"]
# "auto" self-escalates from the cheap HTTP lane to the slow Playwright/
# patchright lane once a run confirms a block; "http"/"browser" are an
# explicit operator override that always wins (task-50 §B4).
FetchMode = Literal["auto", "http", "browser"]
# "auto" self-detects via `/wp-json/` namespaces and caches the verdict;
# "redirection"/"probe" pin a source explicitly; "off" disables collection
# for this client without touching the global `monitor_redirects_enabled`
# (task-51 §3.2.4).
RedirectSourceChoice = Literal["auto", "redirection", "probe", "off"]
RedirectSource = Literal["redirection", "probe"]
RedirectListState = Literal["active", "removed", "all"]
CredentialsStatus = Literal["configured", "missing", "invalid"]


class MonitorSettingsDTO(BaseModel):
    client_id: str
    enabled: bool
    base_url_override: str | None = None
    # `None` = not known whether the site canonically redirects to `www.` or
    # bare — see `MonitorSettings.canonical_www`.
    canonical_www: bool | None = None
    light_interval_hours: float | None = None
    full_interval_hours: float | None = None
    max_urls: int | None = None
    monitor_when_paused: bool
    ignore_patterns: list[str] = []
    muted_change_types: list[str] = []
    fetch_mode: FetchMode = "auto"
    # Read-only — when `fetch_mode="auto"` last self-escalated to the
    # browser lane. `None` if never blocked, or an explicit mode overrides
    # the escalation entirely.
    blocked_detected_at: datetime | None = None
    redirects_enabled: bool = True
    redirect_source: RedirectSourceChoice = "auto"
    # Read-only — `None` if redirects have never been successfully collected
    # for this client (task-51 §3.5 baseline gate).
    redirects_last_checked_at: datetime | None = None
    updated_at: datetime


class MonitorSettingsUpdateRequest(BaseModel):
    """`PATCH /clients/{id}/monitor/settings` — every field optional, only
    what's sent is changed (mirrors `ClientUpdateRequest`)."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool | None = None
    base_url_override: str | None = None
    canonical_www: bool | None = None
    light_interval_hours: float | None = None
    full_interval_hours: float | None = None
    max_urls: int | None = None
    monitor_when_paused: bool | None = None
    ignore_patterns: list[str] | None = None
    muted_change_types: list[str] | None = None
    fetch_mode: FetchMode | None = None
    redirects_enabled: bool | None = None
    redirect_source: RedirectSourceChoice | None = None


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
    # `None` for a run that never hit the antibot lane at all (nothing was
    # ever attempted, so there is nothing to say "0 blocked" about — the
    # same `None` != `0` invariant as `urls_total`) vs. `0` for a run that
    # ran the block-detection path and found nothing (task-50 §4).
    blocked_count: int | None = None


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
    # `MonitorPage.id` for `scope="page"` changes — lets the client link
    # straight to that page's diff viewer instead of a dead-end row.
    related_id: str | None = None
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
    kind: Literal["robots", "sitemap", "redirects"]
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


# --- redirects (task-51) ------------------------------------------------


class MonitorRedirectDTO(BaseModel):
    id: str
    source: RedirectSource
    source_url: str
    target_url: str | None = None
    http_code: int | None = None
    match_type: str
    regex: bool
    status: Literal["enabled", "disabled"]
    group_name: str | None = None
    title: str | None = None
    external_id: int | None = None
    # The whole point of this table (task-51 §3.1/§3.5): the plugin itself
    # stores neither — our own hourly check is the only source of a date.
    first_seen_at: datetime
    # Lower bound of "appeared between X and Y" — `None` when unknown
    # (baseline, or the previous check failed), never a guessed date
    # (PLAN §12).
    first_seen_window_start: datetime | None = None
    last_seen_at: datetime
    removed_at: datetime | None = None
    version_seq: int


class MonitorRedirectsPageDTO(BaseModel):
    redirects: list[MonitorRedirectDTO]
    total: int
    limit: int
    offset: int


class MonitorRedirectsStatusDTO(BaseModel):
    """`GET /clients/{id}/monitor/redirects/status` — deliberately never

    carries the Application Password itself (task-51 §3.3): `credentials_status`
    is the only thing this DTO says about it.
    """

    client_id: str
    redirect_source: RedirectSourceChoice
    # The source actually used the last time collection ran — `None` if
    # `redirect_source="off"` or nothing has run yet.
    effective_source: RedirectSource | None = None
    credentials_status: CredentialsStatus
    last_checked_at: datetime | None = None
    rules_total: int
    rules_enabled: int
    rules_disabled: int
    last_unavailable_reason: str | None = None
