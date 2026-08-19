"""DTOs for the Reports engine (task-42 §5), prefix `/api/v1/reports`.

Follows `app/schemas/monitor.py`'s conventions: `Literal[...]` aliases
instead of `enum.Enum` (matches the DB layer's plain-`String` columns — a
new value never needs a migration), write DTOs get
`ConfigDict(extra="forbid")`, read DTOs don't.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

ReportKind = Literal["monthly", "yearly", "custom_months", "comparator"]
ReportSource = Literal["api", "pdf"]
ReportStatus = Literal["queued", "running", "succeeded", "partial", "failed", "cancelled"]
CrossCheckStatus = Literal["match", "within_tolerance", "mismatch"]
YearlySpan = Literal["ytd", "full"]
FileKind = Literal["pdf", "xlsx", "screenshot", "client_pdf"]
PeriodState = Literal["waiting", "running", "done", "cached", "failed", "skipped"]


class ReportTypeEstimateDTO(BaseModel):
    api_seconds_per_column: float
    pdf_seconds_per_column: float


class ReportTypeDTO(BaseModel):
    kind: ReportKind
    label: str
    estimate: ReportTypeEstimateDTO


class ReportTypesDTO(BaseModel):
    """`GET /reports/types` (task-42 §5) — the frontend never hardcodes
    these limits or per-column time estimates.
    """

    types: list[ReportTypeDTO]
    max_months: int
    max_comparator_years: int


class ReportRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_id: str
    kind: ReportKind
    source: ReportSource
    years: list[int] | None = None
    span: YearlySpan | None = None
    months: list[str] | None = None
    force_refresh: bool = False


class ReportRunAcceptedDTO(BaseModel):
    run_id: str
    status_url: str
    events_url: str


class ReportRunPeriodDTO(BaseModel):
    """One period's own progress (task-45 §2.1), read from
    `ReportRun.periods_json`. `date_from`/`date_to` are known before the run
    starts, so the UI can label every period from the first second; `state`
    replaces task-43's log-text heuristic entirely.
    """

    label: str
    date_from: date | None = None
    date_to: date | None = None
    is_partial: bool = False
    state: PeriodState = "waiting"
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_seconds: float | None = None
    from_cache: bool = False
    # Additive (task-49 §2.6): the date the reused PDF was ORIGINALLY
    # downloaded, distinct from this run's own `finished_at` — the whole point
    # is showing the operator how stale the source file is, not when the
    # cache lookup happened.
    cached_at: date | None = None
    error: str | None = None
    # Additive (task-47): which attempt this state reflects, and the retry
    # budget for this period's source — lets the UI show "Running — retry
    # 2/3" live and "Failed (3/3 attempts)" on exhaustion, instead of a bare
    # state word that looked identical whether it failed once or three times.
    attempt: int = 1
    max_attempts: int = 1


class ReportRunDTO(BaseModel):
    id: str
    client_id: str
    kind: ReportKind
    source: ReportSource
    status: ReportStatus
    period_count: int
    periods_done: int
    periods_failed: int
    cross_check_status: CrossCheckStatus | None = None
    error_code: str | None = None
    error_message: str | None = None
    cancel_requested: bool = False
    # Additive (task-43): `years`/`span`/`months` — never secrets, per
    # `_params_for` in `app/api/reports.py` — so the UI's "Retry" action
    # (task-43 §4) can rebuild the exact same request instead of only ever
    # being able to retry `kind=monthly`, which alone needs no parameters.
    params: dict[str, Any] = Field(default_factory=dict)
    # Additive (task-45 §2.1): per-period state, the only source the UI's
    # progress list reads. Empty for runs created before the `periods_json`
    # migration — never a fabricated "all waiting" list.
    periods: list[ReportRunPeriodDTO] = Field(default_factory=list)
    # Additive (task-45 §2.2): the Reporting Ninja report name currently bound
    # to this run's client, so a wrong binding is visible in the run header
    # instead of only in the database. Filled by the single-run endpoints only
    # (`GET /runs/{id}`, `POST /runs/{id}/cancel`) — the list endpoint leaves
    # it `None` rather than issuing one integration lookup per row.
    ninja_report_name: str | None = None
    # Additive (task-47 §4): `None` for a standalone run, the parent batch's
    # id for one created by `POST /reports/batches` — lets the run detail
    # page and the history filter's `client=<id>` case (task-47 §3.5 point 7)
    # link back to the batch a client's own run belongs to.
    batch_id: str | None = None
    queued_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None


class ReportRunsPageDTO(BaseModel):
    rows: list[ReportRunDTO] = []
    total: int = 0
    limit: int
    offset: int


class ReportTableCellSourceDTO(BaseModel):
    period_label: str
    file: str | None = None


class ReportTableRowDTO(BaseModel):
    key: str
    label: str
    values: list[int | None]
    changes: list[float | None]


class ReportTablePeriodDTO(BaseModel):
    label: str
    date_from: date
    date_to: date
    is_partial: bool


class ReportHistoryMonthDTO(BaseModel):
    """One complete calendar month of the Historical Data block (task-45 §2.4).

    A month the provider has no data for is `None` across the board — never a
    row of zeros with a 0.00% CTR (task-45 §1.6/PLAN §12). `ctr` is derived
    (`clicks / impressions`), matching Reporting Ninja's own `ctr` field.
    """

    month: str  # YYYY-MM
    label: str  # "July 2026"
    clicks: int | None = None
    impressions: int | None = None
    ctr: float | None = None
    organic_conversions: int | None = None


class ReportHistoryDTO(BaseModel):
    months: list[ReportHistoryMonthDTO] = []
    has_gsc: bool = True
    has_ga4: bool = True


class ReportTableDTO(BaseModel):
    """`GET /reports/runs/{id}/table` (task-42 §5): the finished 8xN table
    plus per-period provenance ("походження кожної клітинки" — which file a
    whole column was read from; empty on the API path).
    """

    run_id: str
    kind: ReportKind
    source: ReportSource
    periods: list[ReportTablePeriodDTO]
    change_labels: list[str]
    rows: list[ReportTableRowDTO]
    sources: dict[str, str] = {}
    cross_check: dict[str, Any] | None = None
    # Additive (task-45 §2.4): the monthly history block shown under the main
    # table and written as its own XLSX sheet. `None` when the history query
    # could not run at all — the main table still stands on its own.
    history: ReportHistoryDTO | None = None
    # Additive (task-49 §2.6): which periods (by label) were served from a
    # cached PDF rather than freshly downloaded, and the oldest of their
    # original download dates — empty/`None` for an API-source run or a PDF
    # run where nothing was reused. The oldest date is what determines the
    # report's real freshness, not the run's own `finished_at`.
    cached_periods: list[str] = Field(default_factory=list)
    oldest_cached_at: date | None = None


class ReportFileDTO(BaseModel):
    id: str
    run_id: str | None = None
    kind: FileKind
    period_label: str | None = None
    date_from: date | None = None
    date_to: date | None = None
    is_partial_period: bool = False
    size_bytes: int
    mime: str | None = None
    created_at: datetime


class NinjaReportCandidateDTO(BaseModel):
    name: str
    exact_match: bool


class NinjaReportNameDTO(BaseModel):
    """`GET/PUT /reports/clients/{id}/ninja-report`.

    task-45 §2.2 splits what task-42 returned as one flat, sortable
    `candidates` list: `matches` are the account's reports whose name starts
    with the client's name, `others` is the rest of the account. Sorting alone
    stopped nothing — an operator could bind another client's report to this
    client and every later PDF run would then parse 8/8 plausible but foreign
    metrics under this client's name.
    """

    client_id: str
    client_name: str | None = None
    ninja_report_name: str | None = None
    matches: list[NinjaReportCandidateDTO] = Field(default_factory=list)
    others: list[NinjaReportCandidateDTO] = Field(default_factory=list)
    # Additive (task-48 §2.1): `others` comes from a scroll-until-stable pass
    # over the cabinet's own table that can still run out of iteration
    # budget on a very long account. `True` means "not proven complete" —
    # the UI must never read a name's absence from `others` as "this report
    # does not exist" while this is set.
    others_truncated: bool = False


class NinjaReportNameUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ninja_report_name: str
    # Binding a report whose name does not start with the client's name is
    # possible but never accidental (task-45 §2.2): without this flag the API
    # answers `422 report_name_mismatch`, so the UI checkbox is a second
    # safeguard, not the only one.
    confirm_mismatch: bool = False


# --- GET /ninja-bindings (task-49 §2.1) ---------------------------------
#
# One cheap, DB-only pass over every client so the wizard can show who is
# ready for a `source=pdf` batch BEFORE Start is pressed — the live incident
# this task closes was 9/12 clients failing at 0 seconds for a reason that
# was sitting in the database the whole time. Deliberately a NEW, separately
# named state set from task-48's `BindingStatus` (`ok|foreign|missing|
# not_configured`): that one serves the accuracy/audit report, this one
# gates the Start button, and the task doc's own contract names these four
# values.

NinjaBindingState = Literal["ready", "no_report_name", "not_connected", "foreign_binding"]


class NinjaBindingRowDTO(BaseModel):
    client_id: str
    name: str
    domain: str | None = None
    connected: bool
    ninja_report_name: str | None = None
    state: NinjaBindingState


class NinjaBindingsDTO(BaseModel):
    rows: list[NinjaBindingRowDTO] = Field(default_factory=list)


# --- Rebuild (task-49 §2.4/§2.5) -----------------------------------------
#
# `force_refresh` used to be inherited silently from the original run's own
# params whenever an operator hit "Retry" (task-49 §1.4) — including for the
# exact client being retried BECAUSE the data was wrong. Both request bodies
# below make the caller state it explicitly every time.


class RunRebuildRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    force_refresh: bool = True


class BatchClientRetryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    force_refresh: bool = False


# --- Batches (task-47 §4/§5) --------------------------------------------
#
# A batch is a parent row over N ordinary `ReportRun`s (task-47 §2.2) — every
# batch DTO below is deliberately thin (status + counters + a light per-client
# row), never a duplicate of `ReportRunDTO`'s own periods/table/files: the
# batch page links out to the existing run detail page for that (task-47
# §2.4 "прогрес одночасно є навігацією по результату").


class ReportBatchCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_ids: list[str]
    kind: ReportKind
    source: ReportSource
    years: list[int] | None = None
    span: YearlySpan | None = None
    months: list[str] | None = None
    force_refresh: bool = False


class ReportBatchAcceptedDTO(BaseModel):
    batch_id: str
    status_url: str
    events_url: str


class ReportBatchClientRowDTO(BaseModel):
    """One row of the batch progress matrix (task-47 §2.4/§3.4) — enough to
    render "4/6 periods, Running, 0:42" without a second request per client;
    "Open document"/"Client PDF" link to the existing run-detail routes.
    """

    run_id: str
    client_id: str
    client_name: str | None = None
    status: ReportStatus
    period_count: int
    periods_done: int
    periods_failed: int
    error_message: str | None = None
    # Additive (task-49 §2.3/§2.4): lets the matrix show `Connect Reporting
    # Ninja` instead of `Retry` for exactly this failure, and nothing else —
    # a retry cannot fix a missing binding, so offering it is a guaranteed
    # second failure.
    error_code: str | None = None
    # Additive (task-49 §2.6): how many of this client's periods were served
    # from a cached PDF — `cached_count == period_count` is the `From cache`
    # chip; anything in between is `N cached / M downloaded`.
    cached_count: int = 0
    started_at: datetime | None = None
    finished_at: datetime | None = None


class ReportBatchDTO(BaseModel):
    id: str
    kind: ReportKind
    source: ReportSource
    status: ReportStatus
    client_count: int
    clients_done: int
    clients_failed: int
    cancel_requested: bool = False
    params: dict[str, Any] = Field(default_factory=dict)
    clients: list[ReportBatchClientRowDTO] = Field(default_factory=list)
    queued_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None


class ReportBatchesPageDTO(BaseModel):
    rows: list[ReportBatchDTO] = []
    total: int = 0
    limit: int
    offset: int


# --- Accuracy (task-48 §2.4) --------------------------------------------
#
# `GET /reports/accuracy` machine-builds the same table task-48 §1 had to
# assemble by hand from the database and a live Reporting Ninja check — one
# row per client, so "is our data actually right" stops being a question
# that needs a fresh investigation every time someone asks it.

BindingStatus = Literal["ok", "foreign", "missing", "not_configured"]


class AccuracyClientRowDTO(BaseModel):
    client_id: str
    client_name: str
    ninja_report_name: str | None = None
    binding_status: BindingStatus
    has_gsc: bool
    # The most recent `source=pdf` run with a stored cross-check, if any —
    # `None` fields below mean "no PDF run has cross-checked this client
    # yet", never "checked and found nothing wrong".
    latest_pdf_run_id: str | None = None
    cross_check_status: CrossCheckStatus | None = None
    match_count: int = 0
    no_data_count: int = 0
    differ_count: int = 0
    checked_at: datetime | None = None


class AccuracyReportDTO(BaseModel):
    generated_at: datetime
    clients: list[AccuracyClientRowDTO] = Field(default_factory=list)
