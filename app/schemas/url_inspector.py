"""URL Inspector DTOs (task-61) — the shape the header URL search returns.

New DTOs only; nothing existing is changed, so this is not a contract
change for task-02. Provider/metric names below reuse the exact strings the
rest of the app already uses for them (CLAUDE.md: one name, one thing,
everywhere) rather than inventing url-inspector-local synonyms.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict

from app.schemas.enums import Quality

# "has_data" — this source has rows for this URL.
# "no_data" — the source is configured/collected for this client, but this
#   URL isn't in it (a real, common state — task-61 §0.7, e.g. 3 clients
#   have zero GSC page rows at all).
# "not_configured" — this client has no integration for this source at all
#   (PLAN §12: never conflated with "no rows for this URL").
# "failed" — the last collection attempt for this source errored.
# "unknown_client" — no client could be resolved for this URL's host at all,
#   so no source-configuration state can be known.
UrlSourceState = Literal["has_data", "no_data", "not_configured", "failed", "unknown_client"]

Granularity = Literal["week", "month", "quarter"]
AbsenceReason = Literal["truncated", "not_collected"]


class UrlSourceStateDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str  # "gsc" | "reporting_ninja" | "monitor" | "archive" | "page_inventory"
    # | "tech_audit"
    state: UrlSourceState
    detail: str | None = None


class ResolvedClientDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    slug: str
    domain: str
    logo_path: str | None = None
    match: Literal["exact", "subdomain"]


class UrlSeriesPointDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")

    period_start: date
    period_end: date
    period_days: int
    value: Decimal | None
    # Present only when `value` is `None` — the period was collected but
    # this URL legitimately has no row in it (PLAN §12: never a fabricated
    # 0). `truncated` = the period's own top-N cutoff excluded this URL,
    # which is the common case (task-61 §0.4: 83% of GSC page rows on this
    # portfolio carry `quality=truncated`). `not_collected` = the period
    # exists for other URLs but never touched this one.
    absence_reason: AbsenceReason | None = None
    quality: Quality | None = None
    observed_at: datetime | None = None


class UrlSeriesDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")

    metric: str
    unit: Literal["count", "ratio", "position"]
    granularity: Granularity
    points: list[UrlSeriesPointDTO] = []
    # Periods that exist for this URL but at a length outside the requested
    # granularity's bucket (task-61 §0.4) — never silently folded onto the
    # chart, only counted so the UI can say "N more periods of other
    # lengths exist".
    other_length_periods: int = 0


class UrlSearchAnalyticsDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")

    gsc: UrlSourceStateDTO
    reporting_ninja: UrlSourceStateDTO
    gsc_series: dict[str, UrlSeriesDTO] = {}
    ga4_series: dict[str, UrlSeriesDTO] = {}


class UrlArchiveSnapshotDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    observed_at: datetime
    http_status: int | None
    final_url: str | None
    store_reason: str
    title: str | None
    has_change: bool


class UrlArchiveDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")

    state: UrlSourceStateDTO
    archive_url_id: str | None = None
    snapshots: list[UrlArchiveSnapshotDTO] = []
    snapshot_count: int = 0


class UrlChangeDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    severity: str
    change_type: str
    old_value: str | None
    new_value: str | None
    detected_at: datetime
    resolved_at: datetime | None
    acknowledged_at: datetime | None


class UrlMonitorPageVersionDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    observed_at: datetime
    http_status: int | None
    changed_fields: list[str] = []


class UrlMonitorDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")

    state: UrlSourceStateDTO
    page_id: str | None = None
    status: int | None = None
    indexable: bool | None = None
    canonical: str | None = None
    meta_robots: str | None = None
    final_url: str | None = None
    hop_count: int | None = None
    last_checked_at: datetime | None = None
    gone_since: datetime | None = None
    changes: list[UrlChangeDTO] = []
    changes_total: int = 0
    versions: list[UrlMonitorPageVersionDTO] = []
    versions_total: int = 0


class UrlFindingDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")

    check: str
    field: str
    severity: str
    message: str


class UrlMetaDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")

    state: UrlSourceStateDTO
    # Where title/description/h1 below were read from — the operator needs
    # to know how fresh this is, since it's never a live fetch on this
    # endpoint (task-61 §2.3).
    meta_source: Literal["page_inventory", "monitor"] | None = None
    title: str | None = None
    description: str | None = None
    h1: str | None = None
    keywords: list[str] = []
    findings: list[UrlFindingDTO] = []


class UrlTechFindingDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")

    check: str
    category: str | None
    severity: str
    detail: str | None
    how_to_fix: str | None


class UrlTechAuditDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")

    state: UrlSourceStateDTO
    audit_date: str | None = None
    findings: list[UrlTechFindingDTO] = []


class UrlInspectorDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input_url: str
    inspector_key: str
    client: ResolvedClientDTO | None
    client_match: Literal["exact", "subdomain", "unknown_domain"]
    coverage: list[UrlSourceStateDTO]
    search_analytics: UrlSearchAnalyticsDTO
    archive: UrlArchiveDTO
    monitor: UrlMonitorDTO
    meta: UrlMetaDTO
    tech_audit: UrlTechAuditDTO
    generated_at: datetime


# --- live micro tech audit (POST /url-inspector/live) ---------------------


class UrlInspectorLiveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str
    with_schema: bool = False


class UrlLiveHopDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")

    index: int
    url: str
    status: int
    location: str | None = None


class UrlLiveSignalsDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = None
    description: str | None = None
    h1: str | None = None
    canonical: str | None = None
    meta_robots: str | None = None
    x_robots_tag: str | None = None
    hreflang: list[list[str]] = []
    json_ld_types: list[str] = []
    word_count: int | None = None
    analytics_ids: list[str] = []


class UrlInspectorLiveDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input_url: str
    final_url: str | None = None
    http_status: int | None = None
    redirect_chain: list[UrlLiveHopDTO] = []
    # "ok" | "blocked" | "unsafe_url" | "fetch_failed" | "timeout"
    verdict: str
    indexable: bool | None = None
    signals: UrlLiveSignalsDTO | None = None
    findings: list[UrlFindingDTO] = []
    schema_check_id: str | None = None
    error: str | None = None
    generated_at: datetime


# --- type-ahead suggestions (GET /url-inspector/suggest) -------------------


class UrlSuggestResponseDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")

    suggestions: list[str] = []
