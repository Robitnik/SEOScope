"""Компактні вихідні моделі tools (план §7.2): "обрізати агресивно".

Свідомо ОКРЕМІ від `app/schemas/api.py`/`app/schemas/*.py` — ті описують
повну форму REST-відповіді для UI (десятки полів на клієнта), а модель у
чаті платить за кожен зайвий токен контексту. Кожна модель тут — це те, що
відповідає на конкретне питання відділу (план §0), плюс `None`, коли даних
нема (ніколи `0` — інваріант проєкту переживає й це звуження).

Кожен клас успадковує `BaseModel`, і функція-tool повертає його екземпляр
напряму: SDK (`func_metadata`) сама будує `outputSchema` з анотації
повернення і заповнює і `structuredContent`, і текстовий дубль для
зворотної сумісності (див. task-74 §7.2) — тут нічого руками серіалізувати
не треба.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel

# --- A. Орієнтація ----------------------------------------------------------


class ClientSummary(BaseModel):
    client_id: str
    name: str
    domain: str
    status: str
    is_dev: bool


class ProviderStatusSummary(BaseModel):
    provider: str
    status: str
    last_synced_at: str | None = None


class ClientOverview(BaseModel):
    client_id: str
    name: str
    domain: str
    status: str
    overall_status: str
    last_synced_at: str | None = None
    providers: list[ProviderStatusSummary] = []
    metrics: dict[str, Any] = {}


class PortfolioBucket(BaseModel):
    status: str
    count: int
    clients: list[str]


class PortfolioOverview(BaseModel):
    period: str
    generated_at: str
    buckets: list[PortfolioBucket]


# --- B. Веб-архів ------------------------------------------------------------


class ArchivedUrlMatch(BaseModel):
    url_id: str
    client_name: str
    url: str
    snapshot_count: int
    last_captured_at: str | None = None


class ArchivedUrlSearchResult(BaseModel):
    query: str
    matches: list[ArchivedUrlMatch]


class SnapshotSummary(BaseModel):
    snapshot_id: str
    observed_at: str
    http_status: int | None = None
    title: str | None = None
    has_change: bool


class UrlSnapshotTimeline(BaseModel):
    url: str
    client_name: str
    snapshots: list[SnapshotSummary]  # newest first


class UrlVersionComparison(BaseModel):
    url: str
    from_date: str
    to_date: str
    change_kinds: list[str]
    text_delta_words: int
    diff_lines: list[str]


class ArchiveChangeItem(BaseModel):
    client_name: str
    url: str
    detected_at: str
    change_kinds: list[str]
    text_delta_words: int


class ArchiveChangesFeed(BaseModel):
    total: int
    items: list[ArchiveChangeItem]


class CaptureAccepted(BaseModel):
    capture_id: str
    poll_with: Literal["get_job_status"] = "get_job_status"
    kind: Literal["archive_capture"] = "archive_capture"


# --- C. Site Monitor ---------------------------------------------------------


class SiteChangeItem(BaseModel):
    client_name: str
    severity: str
    change_type: str
    subject: str
    old_value: str | None = None
    new_value: str | None = None
    detected_at: str


class SiteChangesFeed(BaseModel):
    total: int
    items: list[SiteChangeItem]


class MonitorSummary(BaseModel):
    client_name: str
    pages_tracked: int
    pages_broken: int
    last_run_at: str | None = None
    last_run_status: str | None = None


class PageDiff(BaseModel):
    page_url: str
    from_version_id: str
    to_version_id: str
    changed_fields: list[str]
    text_diff: list[str]


class RedirectsStatus(BaseModel):
    client_name: str
    total_redirects: int
    last_checked_at: str | None = None


class MonitorRunAccepted(BaseModel):
    run_id: str
    client_id: str
    poll_with: Literal["get_job_status"] = "get_job_status"
    kind: Literal["monitor_run"] = "monitor_run"


# --- D. Звіти -----------------------------------------------------------------


class ReportTableRow(BaseModel):
    # `metric_key` matches `catalog://metrics` entries — `changes` below are
    # RAW percentage deltas with no semantic verdict attached (unlike the
    # dashboard's `MetricCardValue`, this table has no `better_direction`/
    # `is_improvement` of its own). Read the resource to know whether a
    # positive `changes` value is good or bad for this row.
    metric_key: str
    label: str
    values: list[Any]
    changes: list[float | None]


class ReportData(BaseModel):
    run_id: str
    kind: str
    source: str
    periods: list[str]
    rows: list[ReportTableRow]
    # Reminder carried in the payload itself, not just the tool description —
    # it survives even if the model only skims the docstring once per session.
    note: str = (
        "`changes` are raw percentage deltas with no built-in good/bad verdict. "
        "Read the catalog://metrics resource and check each row's better_direction "
        "before describing a change as an improvement or a regression."
    )


class ReportRunSummary(BaseModel):
    run_id: str
    client_id: str
    kind: str
    status: str
    period_count: int
    periods_done: int


class ReportRunAccepted(BaseModel):
    run_id: str
    poll_with: Literal["get_job_status"] = "get_job_status"
    kind: Literal["report_run"] = "report_run"


# --- F. Аналітика --------------------------------------------------------------


class MonthlyRow(BaseModel):
    month: str
    label: str
    organic_users: int | None
    organic_conversions: int | None
    key_event_count: int | None
    gsc_clicks: int | None
    gsc_impressions: int | None
    is_partial_period: bool
    # Семантика зміни визначається `better_direction` метрики, а не знаком
    # числа (CLAUDE.md-інваріант) — лишаємо напрямки як віддав API, а не
    # виводимо їх заново тут.
    directions: dict[str, str | None]


class MonthlyPerformance(BaseModel):
    client_name: str
    collected_at: str | None
    rows: list[MonthlyRow]


# --- H. Операційне -------------------------------------------------------------


class JobStatusReport(BaseModel):
    job_id: str
    kind: str
    status: str
    progress_percent: float | None = None
    error_message: str | None = None
    done: bool
    read_result_with: str | None = None
