"""REST API v1 request/response DTOs (PLAN §10).

These shapes are the contract between `task-06` (routers) and `task-12`
(frontend); OpenAPI generated from these is the source of truth for both.

Design rule carried over from PLAN §12: the backend computes business
semantics (delta, quality, `not_configured` vs `unavailable` vs `failed`,
which direction is an improvement) and hands the frontend a value that is
ready to render. The frontend must not recompute any of this.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.adapter import DateRange, PreflightResult
from app.schemas.enums import (
    BetterDirection,
    CellStatus,
    ClientStatus,
    ComparisonMode,
    JobStatus,
    MetricUnit,
    PeriodKind,
    PeriodRole,
    Provider,
    Quality,
    SourceMode,
    StageStatus,
)

# Composed view state for a provider card/status cell — not a stored value,
# so it lives here as a Literal rather than in app.schemas.enums. Combines
# `Quality` outcomes with the pre-attempt/auth states from PLAN §9.2/§12.
ProviderCardState = Literal[
    "not_configured",
    "auth_required",
    "unavailable",
    "failed",
    "complete",
    "partial",
    "truncated",
    "stale",
]


class ErrorDTO(BaseModel):
    """Safe error payload — never contains secrets, stack traces or paths."""

    error_code: str
    message: str
    details: dict[str, Any] = {}


# --- Dashboard -------------------------------------------------------------


class MetricCardValue(BaseModel):
    """One metric on a client card: fully rendered, no frontend math needed."""

    metric_key: str
    label: str
    unit: MetricUnit
    comparison_kind: Literal["range", "snapshot"]
    precision: int = 0
    is_derived: bool = False

    current_value: Decimal | None = None
    current_text: str | None = None
    previous_value: Decimal | None = None
    previous_text: str | None = None

    delta_abs: Decimal | None = None
    delta_pct: Decimal | None = None
    # Set whenever delta_pct is None despite having a current value, e.g.
    # "previous_zero" or "previous_missing" (PLAN §12) — never "infinity".
    delta_reason: str | None = None

    better_direction: BetterDirection
    # Backend-computed semantic verdict for coloring; None when there is
    # nothing to compare against yet (no previous value).
    is_improvement: bool | None = None

    quality: Quality
    source_mode: SourceMode
    observed_at: datetime | None = None
    data_through: date | None = None
    current_range: DateRange | None = None  # None for snapshot metrics
    previous_range: DateRange | None = None


class ProviderSummaryDTO(BaseModel):
    """One provider's row on a client card."""

    provider: Provider
    state: ProviderCardState
    metrics: list[MetricCardValue] = []
    quality: Quality | None = None  # None when not_configured
    source_mode: SourceMode | None = None
    last_success_at: datetime | None = None
    last_attempt_at: datetime | None = None
    last_error_code: str | None = None
    warnings: list[str] = []


class HealthPointDTO(BaseModel):
    """One day's composite health score.

    Days with no contributing service are absent from the series rather than
    present with a `0` — a consumer must render a gap, not a crash to zero.
    """

    day: date
    value: Decimal


class ClientHealthDTO(BaseModel):
    """Composite performance signal for a client card (PLAN §21.2).

    `value` is `None` — never `0` — when no service contributed. `coverage_*`
    is shown alongside it so the operator can tell "72% · 3/4 services" apart
    from a client that only has one service configured at all.
    """

    value: Decimal | None = None
    coverage_contributing: int
    coverage_configured: int

    # Trend, added for the card's week-over-week arrow and daily sparkline.
    # All three stay optional so a caller that never recorded history still
    # gets a valid card: `None` means "no comparison basis", never "0% change".
    value_previous: Decimal | None = None
    delta_pct: Decimal | None = None
    history: list[HealthPointDTO] = Field(default_factory=list)


class MetricTrendDTO(BaseModel):
    """Week-over-week trend + daily sparkline for a single derived metric.

    Mirrors `ClientHealthDTO`'s trend fields (same "no basis yet is `None`,
    never a fabricated 0%" rule) without the composite-specific coverage
    bookkeeping, so other card widgets — e.g. GBP website clicks — can reuse
    the same delta-arrow/sparkline UI the health block already draws.
    """

    value: Decimal | None = None
    value_previous: Decimal | None = None
    delta_pct: Decimal | None = None
    history: list[HealthPointDTO] = Field(default_factory=list)


TechAuditCardState = Literal[
    "no_run",
    "queued",
    "running",
    "complete",
    "partial",
    "truncated",
    "failed",
    "stale",
]


class TechAuditCardDTO(BaseModel):
    """Latest measured crawl state shown independently from metric health.

    Counts are finding rows from one crawl snapshot, after applying only the
    rechecks that belong to that same snapshot.  A failed/running newer attempt
    may therefore keep the last valid counts visible instead of fabricating 0.
    """

    state: TechAuditCardState = "no_run"
    run_id: str | None = None
    observed_at: datetime | None = None
    pages_crawled: int | None = None
    pages_discovered: int | None = None
    max_pages: int | None = None
    open_errors: int | None = None
    error_urls: int | None = None
    open_warnings: int | None = None
    open_notices: int | None = None
    fixed_findings: int = 0
    unreachable_findings: int = 0
    total_findings: int | None = None
    quality: Quality | None = None
    active_job_id: str | None = None


class ClientCardDTO(BaseModel):
    client_id: str
    name: str
    slug: str
    domain: str
    logo_path: str | None = None
    market: str | None = None
    timezone: str
    # Комерційний статус (task-34) їде разом із карткою, щоб бейдж «Paused ·
    # since 12 Jul» рендерився з тієї самої відповіді, без другого запиту.
    # Не плутати з `overall_status` нижче — той про стан ДАНИХ.
    status: ClientStatus = ClientStatus.active
    status_changed_at: datetime | None = None
    status_note: str | None = None
    overall_status: Literal["ok", "attention", "stale", "failed_all"]
    providers: list[ProviderSummaryDTO]
    last_synced_at: datetime | None = None
    health: ClientHealthDTO
    gbp_clicks_trend: MetricTrendDTO = Field(default_factory=MetricTrendDTO)
    gsc_clicks_trend: MetricTrendDTO = Field(default_factory=MetricTrendDTO)
    tech_audit: TechAuditCardDTO = Field(default_factory=TechAuditCardDTO)


# Dashboard sort modes (PLAN §21.3). `quality` is the default: clients
# without connected services no longer land at the top by accident.
DashboardSortMode = Literal["quality", "health", "name", "last_sync", "biggest_drop"]


class DashboardResponse(BaseModel):
    period_kind: PeriodKind
    comparison_mode: ComparisonMode
    current_range: DateRange
    previous_range: DateRange
    clients: list[ClientCardDTO]
    generated_at: datetime


# --- Clients & integrations -------------------------------------------------


class ClientDTO(BaseModel):
    id: str
    name: str
    slug: str
    domain: str
    logo_path: str | None = None
    market: str | None = None
    timezone: str
    enabled: bool = True
    status: ClientStatus = ClientStatus.active
    status_changed_at: datetime | None = None
    status_note: str | None = None
    created_at: datetime
    updated_at: datetime


class ClientCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    slug: str
    domain: str
    market: str | None = None
    timezone: str
    enabled: bool = True
    status: ClientStatus = ClientStatus.active


class ClientUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    domain: str | None = None
    market: str | None = None
    timezone: str | None = None
    enabled: bool | None = None
    # `status` тут навмисно відсутній: його міняє лише
    # `POST /clients/{id}/status`, і саме там ставиться `status_changed_at`.
    # Другий шлях запису означав би два місця, де народжується ця дата.


class ClientStatusRequest(BaseModel):
    """Тіло `POST /clients/{id}/status` (task-34)."""

    model_config = ConfigDict(extra="forbid")

    status: ClientStatus
    # Чому пауза («payment stopped 01.07»). Порожній рядок = стерти нотатку.
    note: str | None = Field(default=None, max_length=300)


class IntegrationDTO(BaseModel):
    id: str
    client_id: str
    provider: Provider
    enabled: bool
    source_mode: SourceMode
    # Non-secret external ids only: property/account/project id,
    # database/country, report name. Never API keys/passwords/cookies.
    integration_config: dict[str, Any] = {}
    last_success_at: datetime | None = None
    last_attempt_at: datetime | None = None


class IntegrationUpsertRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    source_mode: SourceMode = SourceMode.auto
    integration_config: dict[str, Any] = {}


class IntegrationDiscoverRequest(BaseModel):
    """Auto-connect one `(client, provider)` pair (task-33)."""

    model_config = ConfigDict(extra="forbid")

    # `False` turns the call into a dry run: candidates are returned, nothing
    # is written. Useful for re-opening the picker without re-connecting.
    apply: bool = True


class IntegrationCandidateDTO(BaseModel):
    """One possible account for a client, ready to be persisted as-is.

    `integration_config` carries only the non-secret external identifiers that
    `IntegrationDTO.integration_config` accepts, so the UI can hand it straight
    back to `PUT /clients/{id}/integrations/{provider}` when the operator picks
    a candidate — no second server round-trip to resolve an opaque id.
    """

    label: str
    detail: str
    confidence: Literal["exact", "strong", "weak"]
    reason: str
    source_mode: SourceMode
    integration_config: dict[str, Any] = {}
    # `False` means "part of the connection's inventory, not a plausible match".
    # The UI shows suggested candidates up front and keeps the rest behind a
    # "show all" toggle and the search box.
    suggested: bool = True


class IntegrationDiscoveryDTO(BaseModel):
    client_id: str
    provider: Provider
    status: Literal[
        "connected", "needs_choice", "not_found", "unavailable", "already_configured"
    ]
    message: str
    applied: IntegrationDTO | None = None
    candidates: list[IntegrationCandidateDTO] = []
    # Result of probing the integration right after writing it, so the report
    # can distinguish "saved" from "saved and verified".
    preflight: PreflightResult | None = None


class AISeoToolsImportRequest(BaseModel):
    """Import only the selected dashboard roster from the fixed checkout."""

    model_config = ConfigDict(extra="forbid")

    client_ids: list[str] | Literal["all"] = "all"
    dry_run: bool = False


class AISeoToolsImportResponse(BaseModel):
    source_available: bool
    dry_run: bool
    scanned_artifacts: int = 0
    imported_artifacts: int = 0
    integrations_configured: int = 0
    outcomes: dict[str, int] = {}
    warnings: list[str] = []


# --- Jobs --------------------------------------------------------------


class SyncJobRequest(BaseModel):
    """Accepts a single client or a batch (PLAN §9.1)."""

    model_config = ConfigDict(extra="forbid")

    client_ids: list[str] | Literal["all"]
    providers: list[Provider] | Literal["all"]
    period_kind: PeriodKind
    comparison_mode: ComparisonMode = ComparisonMode.closed
    anchor: date | None = None
    custom_start: date | None = None
    custom_end: date | None = None
    mode: Literal["collect_missing", "force_refresh"] = "collect_missing"
    source_mode: SourceMode = SourceMode.auto
    idempotency_key: str | None = None

    @model_validator(mode="after")
    def validate_custom_range(self) -> Self:
        if self.period_kind == PeriodKind.custom:
            if self.custom_start is None or self.custom_end is None:
                raise ValueError("custom period requires custom_start and custom_end")
            if self.custom_end < self.custom_start:
                raise ValueError("custom_end must not precede custom_start")
        elif self.custom_start is not None or self.custom_end is not None:
            raise ValueError("custom_start/custom_end are only valid for a custom period")
        return self


class SyncJobAcceptedDTO(BaseModel):
    """`202 Accepted` body for `POST /sync-jobs`."""

    job_id: str | None = None
    batch_id: str | None = None
    status_url: str
    events_url: str
    # Кого відкинуло розгортання `client_ids="all"` через паузу (task-34).
    # Порожній список за замовчуванням; фронт показує це явно, щоб пропуск
    # ніколи не виглядав як тихе зникнення клієнта зі збору.
    skipped_paused_client_ids: list[str] = Field(default_factory=list)


class CellProgressDTO(BaseModel):
    """Progress of one `(client, provider)` cell — PLAN §9.1."""

    client_id: str
    provider: Provider
    status: CellStatus
    quality: Quality | None = None
    progress_percent: float | None = None
    current_stage: str | None = None
    # Free-form live-progress text (e.g. "found 148 posts so far"). Prefer this
    # over `current_stage`'s static per-stage label when present.
    progress_message: str | None = None
    attempt: int = 1
    # Which parallel lane collected this cell (task-31). `None` = a provider
    # running a single lane, or a cell not claimed yet — the progress UI shows
    # per-lane rows only when this is present.
    slot: int | None = None
    error_code: str | None = None
    units_spent: int | None = None
    units_estimated: int | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None


class BatchProgressDTO(BaseModel):
    """Aggregates cells for the frontend's progress matrix (task-12)."""

    job_id: str | None = None
    batch_id: str | None = None
    status: JobStatus
    progress_percent: float | None = None
    cells: list[CellProgressDTO] = []


class JobStepDTO(BaseModel):
    provider: Provider
    stage: str
    status: StageStatus
    progress_percent: float | None = None
    attempt: int = 1
    started_at: datetime | None = None
    finished_at: datetime | None = None
    warning: str | None = None
    error_code: str | None = None
    units_spent: int | None = None
    units_estimated: int | None = None


class JobDTO(BaseModel):
    id: str
    batch_id: str | None = None
    client_id: str
    period_kind: PeriodKind
    comparison_mode: ComparisonMode
    anchor_date: date
    custom_start: date | None = None
    custom_end: date | None = None
    requested_providers: list[Provider]
    status: JobStatus
    progress_percent: float | None = None
    current_stage: str | None = None
    attempt: int = 1
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error_code: str | None = None
    error_message: str | None = None
    steps: list[JobStepDTO] = []
    cells: list[CellProgressDTO] = []


# --- Operations --------------------------------------------------------


class ProviderStatusCellDTO(BaseModel):
    """One `(client, provider)` row for `GET /providers/status` — no secrets."""

    client_id: str
    provider: Provider
    configured: bool
    source_mode: SourceMode | None = None
    state: ProviderCardState
    reason_code: str | None = None
    units_estimated: int | None = None
    last_checked_at: datetime | None = None


class ProvidersStatusDTO(BaseModel):
    generated_at: datetime
    cells: list[ProviderStatusCellDTO]


# --- Browser session bridge (task-20 primitives, task-22 HTTP surface) -----
#
# Mirrors `app.integrations.browser_session.SessionState` as a plain Literal
# rather than importing that module here: `app.integrations` depends on
# `app.schemas`, never the reverse (PLAN §5.2 layering).
SessionLivenessState = Literal["alive", "expired", "no_profile"]


class ProviderLoginDTO(BaseModel):
    """`POST /providers/{provider}/login` response. Neither task-20 (owned
    `check_session`/`start_login` as plain functions, no route) nor task-21
    (owned the auth-pause job-engine plumbing that *consumes* `check_session`)
    put a route on these — task-22's «Log in» button has nothing to call
    without one, so it's added here, additively, against this file's existing
    style."""

    login_session_id: str
    state: SessionLivenessState
    # Echoes `app.jobs.lane.DEFAULT_AUTH_PAUSE_TIMEOUT_SECONDS` — the same
    # number the lane actually waits before giving up — so the UI's countdown
    # can never drift from the real timeout.
    timeout_seconds: int


class ProviderSessionDTO(BaseModel):
    """`GET /providers/{provider}/session` response — a live liveness probe,
    not derived from stored cell history like `ProviderStatusCellDTO`. Used
    by the standalone client-card «Log in» action, which runs outside any
    job/SSE context, and by the sync modal's pre-start browser check."""

    state: SessionLivenessState
    # `state="expired"` collapses two different truths (browser_session.py's
    # `check_session`): a warm Chrome that's up but logged out, vs. no warm
    # Chrome reachable at all. The pre-start check needs to tell those apart —
    # only the second one needs `seo-audit browser-up`, not a login click —
    # so this rides along additively rather than overloading `state` further.
    running: bool


# --- Client metrics history ------------------------------------------------
#
# Added by task-06 (REST API): task-02 left `GET /clients/{id}/metrics`'s
# response shape open for the owning router task to define. Additive only —
# no existing DTO above is touched. See this task's Completion report,
# "Contract changes", for the rationale.


class MetricHistoryPointDTO(BaseModel):
    period_start: date | None = None
    period_end: date | None = None
    observed_at: datetime | None = None
    value_numeric: Decimal | None = None
    quality: Quality


class MetricHistorySeriesDTO(BaseModel):
    metric_key: str
    label: str
    provider: Provider
    unit: MetricUnit
    comparison_kind: Literal["range", "snapshot"]
    # Oldest -> newest.
    points: list[MetricHistoryPointDTO] = []


class ClientMetricsHistoryDTO(BaseModel):
    client_id: str
    period_kind: PeriodKind
    series: list[MetricHistorySeriesDTO]
    generated_at: datetime


class AnalyticsDatasetRowDTO(BaseModel):
    dimension_value: str
    metrics: dict[str, Decimal | None]
    quality: Quality
    rank: int | None = None


class AnalyticsDatasetResponseDTO(BaseModel):
    client_id: str
    provider: Provider
    dataset_key: str
    dimension_key: str | None = None
    period_role: PeriodRole
    period_start: date | None = None
    period_end: date | None = None
    rows: list[AnalyticsDatasetRowDTO] = []
    total_rows_available: int = 0
    truncated: bool = False
    limit: int
    offset: int
    generated_at: datetime


class AhrefsIssueDTO(BaseModel):
    slug: str
    issue_id: str = ""
    name: str
    severity: str
    priority: str
    description: str = ""
    how_to_fix: str = ""
    affected_urls: int = 0
    exported_rows: int = 0


class AhrefsSiteAuditDTO(BaseModel):
    client_id: str
    observed_at: datetime | None = None
    data_through: date | None = None
    project_id: str | None = None
    issues: list[AhrefsIssueDTO] = []
    total_issue_types: int = 0
    total_affected_rows: int = 0
    severity_counts: dict[str, int] = {}
    generated_at: datetime


class AhrefsAffectedUrlDTO(BaseModel):
    url: str
    issue_slug: str
    issue_name: str
    severity: str
    details: dict[str, Any] = {}


class AhrefsAffectedUrlsDTO(BaseModel):
    client_id: str
    issue_slug: str | None = None
    rows: list[AhrefsAffectedUrlDTO] = []
    total_rows_available: int = 0
    limit: int
    offset: int
    generated_at: datetime


# --- Coverage of an aggregated range ---------------------------------------
#
# Added by task-14 (period engine v2). Extended periods (quarter/year/custom)
# are served by aggregating stored history rather than by new collection
# (PLAN §21.1), so the depth of that history is a first-class part of the
# answer: the UI must be able to say "data for 47 of 92 days" instead of
# quietly presenting a hole as a complete period (§12).
# Placed in its own section, appended at the end, so task-15's additions to
# this file (health fields, sort) do not collide with it.


class CoverageDTO(BaseModel):
    """How much of a requested range is actually backed by stored data."""

    days_total: int
    days_covered: int
    # Maximal uncovered sub-ranges, oldest first; empty when complete.
    gaps: list[DateRange] = []
    # `complete` only when `gaps` is empty; `unavailable` when nothing at
    # all was stored for the range — never reported as `complete` with zeros.
    quality: Quality


# --- GSC dimensional analytics (pages/queries) ------------------------------
#
# Added by task-16 (GSC dimensional data, PLAN §21.4): task-02 left this
# response shape open for the owning task to define, same precedent as
# task-06/task-14's additions above. Additive only — nothing existing here
# is touched. See task-16's Completion report, "Contract changes".


class DimensionRowDTO(BaseModel):
    """One page/query row — CTR/position keep GSC's own precision (PLAN §12:
    never truncated to int); `None` means no data, never `0`."""

    dimension_value: str
    clicks: int | None = None
    impressions: int | None = None
    ctr: Decimal | None = None
    position: Decimal | None = None
    quality: Quality
    # 1-based rank in this period's impressions ordering; `None` for the
    # top-N cut's aggregated remainder row (`dimension_value` = "other").
    rank: int | None = None


class DimensionAnalyticsResponseDTO(BaseModel):
    """`GET /clients/{id}/analytics/{pages,queries}` response.

    `truncated` is derived from the stored rows' own `quality`, not
    inferred from row count, so a caller can never mistake a short-but-
    complete list for a cut one (PLAN §12: truncated must never pass as
    the full list). `total_rows_available` is the pre-cap count for the
    matched period, independent of this response's own `limit`/`offset`.
    """

    client_id: str
    dimension: Literal["page", "query"]
    period_start: date | None = None  # None when no stored period matched the request
    period_end: date | None = None
    rows: list[DimensionRowDTO] = []
    total_rows_available: int = 0
    truncated: bool = False
    limit: int
    offset: int
    generated_at: datetime


# --- Alerts: pages/queries declining or rising ------------------------------
#
# Added by task-17 (alerts engine, PLAN §21.5): task-02 left this response
# shape open for the owning task, same precedent as task-06/14/16's additions
# above. Additive only — nothing existing here is touched. See task-17's
# Completion report, "Contract changes".


class AlertEntryDTO(BaseModel):
    """One flagged page/query. `clicks_previous`/`delta_pct` are `None` for
    a `new` entry (no previous-period row exists) — never a fabricated `0`
    or `inf` (PLAN §12)."""

    dimension_value: str
    clicks_current: int | None = None
    clicks_previous: int | None = None
    delta_abs: int
    delta_pct: Decimal | None = None


class AlertsResponseDTO(BaseModel):
    """`GET /clients/{id}/alerts` response.

    `declining`/`rising`/`new` are sorted by absolute click impact, never by
    percentage (PLAN §21.5: a 5000->4000 drop outranks a 20->10 one).
    Period dates are the exact calendar ranges `resolve_periods` resolved,
    always present regardless of whether data exists for them — so the UI
    can show "insufficient data for 6-12 January" instead of a blank state.
    `skipped_reason` is set (with all three lists empty) when either period
    carries `partial`/`truncated` data or has no stored data at all — a
    false alert is worse than no alert.
    """

    client_id: str
    dimension: Literal["page", "query"]
    period_current_start: date
    period_current_end: date
    period_previous_start: date
    period_previous_end: date
    declining: list[AlertEntryDTO] = []
    rising: list[AlertEntryDTO] = []
    new: list[AlertEntryDTO] = []
    skipped_reason: str | None = None
    generated_at: datetime


# --- Technical SEO Audit (task-25) -----------------------------------------
#
# Machine-only: every field below is measured by the crawl/detectors. There is
# no agent-authored copy anywhere in this payload — a finding states the problem
# and the deterministic fix, never a generated rewrite.


class TechAuditRecheckSummaryDTO(BaseModel):
    """How the findings of one category stand after their last re-verification."""

    fixed: int = 0
    still_present: int = 0
    unreachable: int = 0
    skipped: int = 0


class TechAuditCategoryDTO(BaseModel):
    category: str  # stable family key, e.g. "http_errors"
    name: str  # human label, e.g. "HTTP errors (4XX/5XX)"
    severity: str  # Error | Warning | Notice (worst in the category)
    priority: str = "Low"
    url_count: int = 0
    finding_count: int = 0
    description: str = ""
    how_to_fix: str = ""
    recheckable: bool = True
    recheck: TechAuditRecheckSummaryDTO = TechAuditRecheckSummaryDTO()


class TechAuditSummaryDTO(BaseModel):
    client_id: str
    has_run: bool = False
    run_id: str | None = None  # provider_snapshots.id of the crawl
    observed_at: datetime | None = None
    audit_date: str | None = None
    health_score: int | None = None
    cms: str | None = None
    pages_crawled: int | None = None
    quality: str | None = None
    totals: dict[str, Any] = {}
    warnings: list[str] = []
    categories: list[TechAuditCategoryDTO] = []
    open_errors: int | None = None
    error_urls: int | None = None
    open_warnings: int | None = None
    open_notices: int | None = None
    fixed_findings: int = 0
    unreachable_findings: int = 0
    total_findings: int | None = None
    generated_at: datetime


class TechAuditFindingDTO(BaseModel):
    id: str  # stable "<slug>\x1f<url>" key, also used to request a recheck
    category: str
    category_name: str = ""
    slug: str
    name: str
    severity: str
    url: str
    current_value: str = ""
    evidence: str = ""  # measured proof, e.g. which pages share this title
    measured: dict[str, Any] = {}
    how_to_fix: str = ""
    final_url: str = ""
    live_status: str = ""
    recheck_status: str | None = None  # fixed|still_present|unreachable|skipped
    recheck_at: datetime | None = None
    recheckable: bool = True


class TechAuditFindingsDTO(BaseModel):
    client_id: str
    category: str | None = None
    rows: list[TechAuditFindingDTO] = []
    total: int = 0
    limit: int
    offset: int
    generated_at: datetime


class TechAuditFindingRef(BaseModel):
    slug: str
    url: str


class TechAuditRecheckRequest(BaseModel):
    findings: list[TechAuditFindingRef] = []


class TechAuditRunAcceptedDTO(BaseModel):
    job_id: str
    status_url: str
    events_url: str


class TechAuditBatchRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_ids: list[str] | Literal["all"]
    max_pages: int | None = Field(default=None, ge=1, le=10_000)
    idempotency_key: str | None = Field(default=None, max_length=200)


class TechAuditBatchAcceptedDTO(BaseModel):
    job_id: str | None = None
    batch_id: str | None = None
    status_url: str
    events_url: str
    queued_count: int
    skipped_active_client_ids: list[str] = Field(default_factory=list)
    skipped_paused_client_ids: list[str] = Field(default_factory=list)


# --- Client Blogs (task-26) --------------------------------------------------
#
# Machine-only: every field below is measured by the crawl. No agent-authored
# copy — title/description are read from the page, never rewritten.


class BlogsSummaryDTO(BaseModel):
    client_id: str
    has_run: bool = False
    run_id: str | None = None  # provider_snapshots.id of the crawl
    observed_at: datetime | None = None
    blog_url: str | None = None
    post_count: int | None = None  # None (not 0) when there is no run yet
    quality: str | None = None
    warnings: list[str] = []
    generated_at: datetime


class BlogPostDTO(BaseModel):
    url: str
    title: str = ""
    description: str = ""
    word_count: int | None = None
    published_date: str | None = None
    modified_date: str | None = None


class BlogPostsPageDTO(BaseModel):
    client_id: str
    q: str | None = None
    sort: str = "date_desc"
    rows: list[BlogPostDTO] = []
    total: int = 0
    limit: int
    offset: int
    generated_at: datetime


class BlogsRunAcceptedDTO(BaseModel):
    job_id: str
    status_url: str
    events_url: str


# --- Portfolio Trends (task-28) --------------------------------------------
# `GET /api/v1/analytics/portfolio-series`: one line per client for a single
# daily metric (GSC clicks/impressions, GA4 sessions/users), bucketed into
# day/week/month. Read-only, same shape family as `AnalyticsDatasetRowDTO`
# above — `None` means "not collected", never `0` (PLAN §12).


class PortfolioMetricDTO(BaseModel):
    key: str
    label: str
    provider: Provider
    unit: MetricUnit
    better_direction: BetterDirection
    dataset_key: str
    field: str
    # Added by task-30: how many trailing days this source still revises (GSC
    # ~3, GA4 ~2 — `app.metrics.portfolio.SeriesSpec.lag_days`). `buckets[]`
    # only carries the resulting `provisional` flag, so a consumer that must
    # say "up to date" vs "N days behind" honestly — the trends page's
    # freshness chip — cannot derive this number from the payload otherwise.
    lag_days: int = 0
    # Added by task-44: whether summing this metric's daily values into a
    # week/month bucket is mathematically correct (event counts) or an
    # overstatement (unique counts, e.g. `rn_users` — plan §1.4). Defaults to
    # `True` since every metric before task-44 was event-shaped; the one
    # exception (`rn_users`) is always passed explicitly.
    additive: bool = True


class PortfolioBucketDTO(BaseModel):
    start: date
    end: date
    days_expected: int
    provisional: bool


class PortfolioPointDTO(BaseModel):
    value: Decimal | None
    days_with_data: int
    quality: Quality


class PortfolioClientSeriesDTO(BaseModel):
    client_id: str
    name: str
    domain: str
    points: list[PortfolioPointDTO] = []
    first_day: date | None = None
    last_day: date | None = None
    days_with_data: int = 0
    days_expected: int = 0


class PortfolioExcludedClientDTO(BaseModel):
    """A client left out of `series` — always with a reason, never silently
    dropped (PLAN §12/§21 — absence must stay explainable)."""

    client_id: str
    name: str
    domain: str
    state: Literal["no_data", "not_configured"]
    reason: str


class PortfolioSeriesDTO(BaseModel):
    metric: PortfolioMetricDTO
    bucket: Literal["day", "week", "month"]
    range: DateRange
    buckets: list[PortfolioBucketDTO] = []
    series: list[PortfolioClientSeriesDTO] = []
    excluded: list[PortfolioExcludedClientDTO] = []
    generated_at: datetime
