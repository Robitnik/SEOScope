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


class UrlKeywordTermDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")

    term: str
    count: int
    is_phrase: bool
    in_title: bool
    in_h1: bool
    in_description: bool


class UrlSiteQueryDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str
    clicks: int | None
    impressions: int | None
    is_brand: bool


class UrlKeywordsDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # On-page term extraction's own coverage state.
    state: UrlSourceStateDTO
    on_page: list[UrlKeywordTermDTO] = []
    meta_keywords: list[str] = []
    # Site-level query matching has its OWN state, separate from `state`
    # above — mirrors `UrlSearchAnalyticsDTO`'s two independent states
    # (gsc/reporting_ninja): a page can have on-page terms with no GSC query
    # data yet, or vice versa.
    queries_state: UrlSourceStateDTO
    site_queries: list[UrlSiteQueryDTO] = []


class UrlScreenshotsStateDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Cache-only coverage state — this endpoint NEVER renders a screenshot
    # (task-64 §5): a live capture takes ~20s, and this panel's data must
    # render as fast as every other stored-data panel. `has_data` means a
    # cached set already exists and the frontend should fetch it from
    # `GET /api/v1/url-screenshots`; `no_data` means none has been captured
    # yet and the frontend should offer to capture one.
    state: UrlSourceStateDTO
    preset_count: int = 0


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
    keywords: UrlKeywordsDTO
    screenshots: UrlScreenshotsStateDTO
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


# --- direct Google API panels (task-88) ------------------------------------
#
# Окремі DTO, а не розширення `UrlSearchAnalyticsDTO`: та панель описує
# СЕРІЮ ЗБЕРЕЖЕНИХ періодів нерівної довжини (`other_length_periods`,
# `absence_reason`) — форму, яка існує рівно тому, що дані приходили
# випадковими вікнами. Прямий API віддає денні рядки за точне вікно, тобто
# інший контракт; склеїти їх в один тип означало б тягнути поля, які для
# однієї половини завжди `None`.

GoogleWindowKey = Literal["28d", "90d", "12m", "16m"]
SeriesBucket = Literal["day", "week", "month"]
MetricUnitKey = Literal["count", "ratio", "position", "seconds"]
GoogleTransport = Literal["google_api", "reporting_ninja"]


class UrlGoogleWindowDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: GoogleWindowKey
    start: date
    end: date
    days: int
    bucket: SeriesBucket
    # Початок вікна впирається в 16-місячну стелю Search Console (task-88
    # §0.7). API про стелю НЕ повідомляє — він просто віддає нуль рядків,
    # тож без цього поля UI сказав би "No data" там, де правильна відповідь
    # "за межею історії Search Console".
    at_gsc_history_edge: bool = False
    # `True`, лише якщо вікно ще й обрізане: запитаний початок ліг за стелю.
    clamped_to_gsc_history: bool = False
    requested_start: date


class UrlGoogleMetricDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")

    metric: str
    unit: MetricUnitKey
    # `None` = Google не має даних. Ніколи не 0 (PLAN §12) — для GSC це
    # окремо критично, бо API підсовує синтетичні нулі (task-88 §0.3).
    value: Decimal | None = None


class UrlGoogleSeriesPointDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")

    period_start: date
    period_end: date
    period_days: int
    value: Decimal | None = None
    # День/бакет після `firstIncompleteDate` — Google уже показує його, але
    # добу не закрив. Значення справжнє, але порівнювати з повними періодами
    # не можна.
    provisional: bool = False


class UrlGoogleSeriesDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")

    metric: str
    unit: MetricUnitKey
    bucket: SeriesBucket
    points: list[UrlGoogleSeriesPointDTO] = []


class UrlGoogleQueryDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str
    clicks: Decimal | None = None
    impressions: Decimal | None = None
    ctr: Decimal | None = None  # частка 0..1, як віддає API
    position: Decimal | None = None


class UrlGoogleBreakdownRowDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")

    keys: list[str]
    metrics: list[UrlGoogleMetricDTO] = []


class UrlGoogleBreakdownDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str  # "gsc_device" | "ga4_channel" | ...
    label: str  # UI-копірайт англійською (frontend/AGENTS.md §14)
    source: Literal["gsc", "ga4"]
    dimensions: list[str]
    # Яке саме питання ставить зріз: `landingPage` — про сесії входу,
    # `pagePath` — про перегляди сторінки. Різні числа на тому самому URL,
    # і жодне не "правильніше" (task-88 §0.4).
    scope: str | None = None
    state: UrlSourceStateDTO
    rows: list[UrlGoogleBreakdownRowDTO] = []
    row_limit: int | None = None
    truncated: bool = False
    note: str | None = None


class UrlGoogleSearchTypeDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")

    search_type: str  # web | image | video | news | discover | googleNews
    metrics: list[UrlGoogleMetricDTO] = []
    supports_queries: bool = True


class UrlGoogleKeyEventDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    key_events: Decimal | None = None
    event_count: Decimal | None = None


class UrlGoogleGscDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")

    state: UrlSourceStateDTO
    totals: list[UrlGoogleMetricDTO] = []
    series: dict[str, UrlGoogleSeriesDTO] = {}
    queries: list[UrlGoogleQueryDTO] = []
    queries_total: int = 0
    queries_truncated: bool = False
    first_incomplete_date: date | None = None


class UrlGoogleGa4DTO(BaseModel):
    model_config = ConfigDict(extra="forbid")

    state: UrlSourceStateDTO
    # Два незалежні набори тоталів — див. `UrlGoogleBreakdownDTO.scope`.
    landing_totals: list[UrlGoogleMetricDTO] = []
    page_totals: list[UrlGoogleMetricDTO] = []
    series: dict[str, UrlGoogleSeriesDTO] = {}
    key_events: list[UrlGoogleKeyEventDTO] = []
    # Назви key events, налаштованих у property (читаються з `metadata`, не
    # прошиті). Порожній список = конверсій не налаштовано, не помилка.
    key_event_names: list[str] = []


class UrlGoogleCrossCheckDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")

    """Вбудована звірка Google проти Google (task-88 §0.10).

    Ті самі кліки/покази/позиція двома незалежними шляхами: прямий GSC API і
    `organicGoogleSearch*` всередині GA4. Живо збіглися до значущої цифри,
    тому розбіжність тут читається як "зламався наш матчинг URL", а не як
    "розійшлись дані".
    """

    state: UrlSourceStateDTO
    gsc_clicks: Decimal | None = None
    gsc_impressions: Decimal | None = None
    gsc_position: Decimal | None = None
    ga4_clicks: Decimal | None = None
    ga4_impressions: Decimal | None = None
    ga4_position: Decimal | None = None
    # `None`, коли хоч одна сторона відсутня — "не знаємо", не "збігається".
    matches: bool | None = None


class UrlGoogleCoreDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input_url: str
    window: UrlGoogleWindowDTO
    transport: GoogleTransport
    gsc: UrlGoogleGscDTO
    ga4: UrlGoogleGa4DTO
    cross_check: UrlGoogleCrossCheckDTO
    generated_at: datetime


class UrlGoogleBreakdownsDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input_url: str
    window: UrlGoogleWindowDTO
    transport: GoogleTransport
    breakdowns: list[UrlGoogleBreakdownDTO] = []
    search_types: list[UrlGoogleSearchTypeDTO] = []
    generated_at: datetime


class UrlSitemapContentDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: str
    submitted: int | None = None
    # `indexed` навмисно НЕ поле цього DTO: API завжди віддає там 0
    # (перевірено на сайті з 248 поданими URL), і показ нуля читався б як
    # "нічого не проіндексовано" (task-88 §4, п. 23).


class UrlSitemapDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    last_submitted: datetime | None = None
    last_downloaded: datetime | None = None
    is_pending: bool | None = None
    is_sitemaps_index: bool | None = None
    warnings: int | None = None
    errors: int | None = None
    contents: list[UrlSitemapContentDTO] = []


class UrlRichResultTypeDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rich_result_type: str
    items: int


class UrlIndexStatusDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")

    """URL Inspection API (task-88 §3.3), окремим ендпоінтом через 6.6 с.

    Кожне поле може бути `None` — і це означає "Google не сказав", а не
    "все гаразд". `VERDICT_UNSPECIFIED` нормалізується в `None` ще в
    `app/urlinspector/google/index.py`.
    """

    state: UrlSourceStateDTO
    input_url: str
    verdict: str | None = None
    coverage_state: str | None = None
    robots_txt_state: str | None = None
    indexing_state: str | None = None
    page_fetch_state: str | None = None
    last_crawl_time: datetime | None = None
    crawled_as: str | None = None
    google_canonical: str | None = None
    user_canonical: str | None = None
    canonical_mismatch: bool | None = None
    referring_urls: list[str] = []
    sitemap_membership: list[str] = []
    mobile_usability_verdict: str | None = None
    mobile_usability_issues: list[str] = []
    rich_results_verdict: str | None = None
    rich_result_types: list[UrlRichResultTypeDTO] = []
    inspection_result_link: str | None = None
    # Сайтмапи property як цілого — сусідній, дешевий запит, який відповідає
    # на "а чи ця сторінка взагалі має шанс бути поданою".
    sitemaps_state: UrlSourceStateDTO
    sitemaps: list[UrlSitemapDTO] = []
    generated_at: datetime


class UrlRealtimeDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")

    state: UrlSourceStateDTO
    active_users: Decimal | None = None
    # GA4 Realtime не приймає фільтр по сторінці, тому значення завжди по
    # property. Поле існує, щоб UI не міг подати його як "зараз на цій
    # сторінці".
    scope: Literal["property"] = "property"
    generated_at: datetime


class UrlIndexStatusRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str


# --- type-ahead suggestions (GET /url-inspector/suggest) -------------------


class UrlSuggestResponseDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")

    suggestions: list[str] = []
