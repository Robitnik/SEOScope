"""DTO сторінки Tools і інструмента Image Alt Text (task-27).

Новий файл у зоні `app/schemas/` — суто **адитивно**: жоден наявний контракт
не змінюється, тож інші таски цього не відчувають (процес — CLAUDE.md,
«Спільні контракти»). Інструменти не мають нічого спільного з провайдерськими
метриками, тому їхні типи живуть окремо від `app/schemas/api.py`.

Як і всюди в проєкті, бекенд віддає готове до рендеру значення: фронт не
дериває пошуковий фрагмент, не вирішує, чи URL із медіатеки, і не тлумачить
вердикт перевірки.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# Статуси прогресу рядка. `copied` ставить копіювання alt; `applied` — лише
# явне підтвердження оператора або вердикт `match` (ніколи здогадкою).
ItemStatus = Literal["pending", "copied", "applied", "skipped"]

# П'ять окремих вердиктів замість «ок / не ок» (PLAN §12): «не перевіряли»
# (`None`) і «не змогли завантажити» — це різні речі, і жодна з них не
# означає «alt не оновлений».
VerifyVerdict = Literal[
    "match", "mismatch", "empty_alt", "image_not_found_on_page", "fetch_failed"
]

UrlKindDTO = Literal["uploads", "theme_asset", "plugin_asset", "external", "unknown"]

# Стан пошуку точного WordPress `attachment ID` (task-39). `None` (поле не
# заповнене) — «ще не шукали»; `not_searchable` — свідомо не ходили в мережу
# (URL не з uploads); решта — реальна спроба з відповідним результатом.
MediaLookupStatusDTO = Literal[
    "resolved", "ambiguous", "not_found", "rest_unavailable", "not_searchable", "failed"
]
MediaConfidenceDTO = Literal["exact", "filename"]

SourceKindDTO = Literal["xlsx", "csv", "tsv"]

MappableField = Literal["image_url", "new_alt", "page_url", "old_alt"]


# --- каталог інструментів -----------------------------------------------------


class ToolCardDTO(BaseModel):
    """Один блок на сторінці Tools."""

    key: str
    name: str
    summary: str
    tags: list[str] = []
    route: str
    status: Literal["available", "coming_soon"] = "available"
    icon: str
    # Власна статистика інструмента — реальна, не декоративна. `None`, якщо
    # інструменту ще нічим хвалитися (ніколи не `0` заради заповнення).
    stat_label: str | None = None
    stat_value: str | None = None


class ToolsCatalogDTO(BaseModel):
    tools: list[ToolCardDTO]


# --- крок 1: інспекція завантаженого файлу ------------------------------------


class SheetPreviewDTO(BaseModel):
    name: str
    headers: list[str]
    sample_rows: list[list[str]]
    header_row_index: int
    suggested_column_map: dict[str, int]
    warnings: list[str] = []


class AltTextUploadDTO(BaseModel):
    """Що ми побачили у файлі — до створення звіту."""

    upload_id: str
    source_filename: str
    source_kind: SourceKindDTO
    sheets: list[SheetPreviewDTO]
    default_sheet: str
    delimiter: str | None = None
    encoding: str | None = None


# --- крок 2: створення звіту --------------------------------------------------


class AltTextReportCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    upload_id: str
    title: str = Field(min_length=1, max_length=200)
    sheet: str | None = None
    client_id: str | None = None
    # Поле → 0-based індекс колонки. Індекс, а не назва: експорти легко
    # містять порожні або дубльовані заголовки.
    column_map: dict[MappableField, int]


class AltTextReportSummaryDTO(BaseModel):
    id: str
    title: str
    client_id: str | None = None
    client_name: str | None = None
    source_filename: str
    source_kind: SourceKindDTO
    source_sheet: str | None = None
    row_count: int
    skipped_count: int
    status_counts: dict[str, int]
    created_at: datetime
    updated_at: datetime


class AltTextReportDTO(AltTextReportSummaryDTO):
    column_map: dict[str, int]
    warnings: list[str] = []
    verify_counts: dict[str, int] = {}
    # Скільки рядків узагалі можна перевірити (мають сторінку використання).
    verifiable_count: int = 0


# --- рядки --------------------------------------------------------------------


class MediaCandidateDTO(BaseModel):
    """Один кандидат при `ambiguous` — вибір лишається за оператором."""

    id: int
    file: str | None = None
    source_url: str | None = None
    alt_text: str | None = None
    title: str | None = None


class AltTextItemDTO(BaseModel):
    id: str
    row_index: int
    image_url: str
    # Головний результат: те, що оператор вставляє в пошук медіатеки.
    search_key: str
    # Коротший варіант, якщо в назві є схожий на лічильник дубліката хвіст.
    search_key_fallback: str | None = None
    url_kind: UrlKindDTO
    # Які саме суфікси відрізали (`-1024x683`, `-scaled`, `.webp`).
    stripped: list[str] = []
    # Машинні попередження деривації (`external_host`, `resize_query_dropped`).
    derivation_warnings: list[str] = []
    searchable: bool
    new_alt: str
    old_alt: str | None = None
    page_url: str | None = None
    # Одна картинка може стояти на кількох сторінках; перевірка обходить їх
    # по черзі, а оператор може відкрити будь-яку.
    page_urls: list[str] = []
    extra: dict[str, str] = {}
    status: ItemStatus
    status_changed_at: datetime | None = None
    verify_status: VerifyVerdict | None = None
    verify_alt: str | None = None
    verify_http_status: int | None = None
    verify_detail: str | None = None
    verify_checked_at: datetime | None = None
    # WordPress media ID (task-39). `None` = ще не шукали — окремо від
    # `not_found` (шукали, нема) і `not_searchable` (URL не з uploads).
    wp_media_id: int | None = None
    wp_media_status: MediaLookupStatusDTO | None = None
    wp_media_confidence: MediaConfidenceDTO | None = None
    wp_media_alt: str | None = None
    wp_media_admin_url: str | None = None
    wp_media_detail: str | None = None
    wp_media_candidates: list[MediaCandidateDTO] = []
    wp_media_checked_at: datetime | None = None


class AltTextItemsPageDTO(BaseModel):
    report_id: str
    total: int
    limit: int
    offset: int
    rows: list[AltTextItemDTO]


class AltTextItemStatusRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: ItemStatus


class AltTextMediaBatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Стеля 50: кожен рядок — реальний запит(и) до сайту клієнта (PLAN §13).
    limit: int = Field(default=50, ge=1, le=50)


class AltTextMediaBatchDTO(BaseModel):
    """Підсумок пакетного пошуку ID (task-39): скільки рядків у кожному стані."""

    resolved: int = 0
    ambiguous: int = 0
    not_found: int = 0
    rest_unavailable: int = 0
    failed: int = 0
    # Скільки рядків із пакета взагалі не мало сенсу шукати (не-uploads URL).
    skipped: int = 0
    # Скільки uploads-рядків без ID лишилось у звіті ПІСЛЯ цього пакета.
    remaining: int = 0


# --- інструмент Redirect Checker (task-29) -------------------------------------

# Прогрес рядка прогону. `failed` — це «не змогли виконати перевірку», а не
# «ланцюжок поганий»: стан ланцюжка описує `verdict`.
RedirectRunStatus = Literal["queued", "running", "done", "failed"]

# Одинадцять окремих станів замість «ок / не ок» (PLAN §12). `blocked` ніколи не
# показується як `broken`: бот-захист ≠ мертва сторінка.
RedirectVerdict = Literal[
    "ok_direct",
    "ok_redirect",
    "chain",
    "loop",
    "too_many_hops",
    "broken",
    "broken_chain",
    "blocked",
    "unsupported_scheme",
    "unsafe_url",
    "fetch_failed",
]

HopKindDTO = Literal["http", "meta_refresh"]
PermanenceDTO = Literal["permanent", "temporary"]


class RedirectOptionsDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_hops: int = Field(default=20, ge=1, le=25)
    follow_meta_refresh: bool = True
    # Вимкнений за замовчуванням: cache-buster змінює URL, а частина сайтів
    # редіректить залежно від query — тобто зіпсував би саме те, що ми міряємо.
    cache_buster: bool = False


class RedirectHopDTO(BaseModel):
    """Один крок ланцюжка — рівно те, що віддав сервер."""

    index: int
    url: str
    method: str = "GET"
    status: int
    status_text: str = ""
    kind: HopKindDTO = "http"
    # `None` на фінальному хопі: він нікуди не веде.
    permanence: PermanenceDTO | None = None
    location: str | None = None
    # Що саме змінилося на цьому кроці (`scheme_http_to_https`, `www_added`, …).
    changes: list[str] = []
    elapsed_ms: int | None = None
    notes: list[str] = []


class RedirectFinalPageDTO(BaseModel):
    """Факти з фінальної сторінки — лише коли вона віддала HTML."""

    title: str | None = None
    canonical: str | None = None
    meta_robots: str | None = None
    # JS-редіректи stdlib не бачить; це підказка людині, не вердикт.
    possible_js_redirect: bool = False


class RedirectTraceDTO(BaseModel):
    """Результат одного трейсу (`POST /trace` — без запису в БД)."""

    input_url: str
    normalized_url: str
    verdict: RedirectVerdict
    hops: list[RedirectHopDTO] = []
    # Кількість **редіректів** (0 = прямий 200), а не довжина списку хопів.
    hop_count: int = 0
    final_url: str | None = None
    final_status: int | None = None
    flags: list[str] = []
    final_page: RedirectFinalPageDTO | None = None
    error_code: str | None = None
    error_detail: str | None = None
    elapsed_ms: int = 0
    checked_at: datetime | None = None


class RedirectTraceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str = Field(min_length=1, max_length=2048)
    options: RedirectOptionsDTO | None = None


class RejectedUrlDTO(BaseModel):
    """Рядок, який посиланням не є — з причиною, а не тихо викинутий."""

    input_url: str
    error_code: str
    message: str


class RedirectTargetDTO(BaseModel):
    """Рядок прогону. Свідомо **не** підклас `RedirectTraceDTO`.

    Тут `verdict` і `hop_count` можуть бути `None` — рядок ще не перевіряли, і це
    не «редіректів немає» та не «нуль хопів» (PLAN §12). У результату трейсу
    вердикт є завжди, тож зливати ці дві форми в одну означало б послабити
    контракт саме там, де він важливий.
    """

    id: str
    row_index: int
    run_status: RedirectRunStatus
    input_url: str
    normalized_url: str
    # Що ми зробили з вставленим рядком (`scheme_added`, `fragment_dropped`).
    input_notes: list[str] = []
    verdict: RedirectVerdict | None = None
    hops: list[RedirectHopDTO] = []
    hop_count: int | None = None
    final_url: str | None = None
    final_status: int | None = None
    flags: list[str] = []
    final_page: RedirectFinalPageDTO | None = None
    error_code: str | None = None
    error_detail: str | None = None
    elapsed_ms: int | None = None
    # `None` = не перевіряли.
    checked_at: datetime | None = None


class RedirectTargetsPageDTO(BaseModel):
    check_id: str
    total: int
    limit: int
    offset: int
    rows: list[RedirectTargetDTO]


class RedirectCheckCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Сирі рядки з textarea: нормалізацію й дедуплікацію робить сервер, щоб
    # правила були в одному місці, а не подвоєні у фронті.
    urls: list[str] = Field(min_length=1)
    title: str | None = Field(default=None, max_length=200)
    client_id: str | None = None
    options: RedirectOptionsDTO | None = None


class RedirectCheckSummaryDTO(BaseModel):
    id: str
    title: str
    client_id: str | None = None
    client_name: str | None = None
    input_count: int
    target_count: int
    run_status_counts: dict[str, int] = {}
    verdict_counts: dict[str, int] = {}
    created_at: datetime
    updated_at: datetime


class RedirectCheckDTO(RedirectCheckSummaryDTO):
    options: RedirectOptionsDTO
    rejected: list[RejectedUrlDTO] = []
    duplicate_count: int = 0
    truncated: bool = False
