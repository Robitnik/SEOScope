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


class AltTextItemsPageDTO(BaseModel):
    report_id: str
    total: int
    limit: int
    offset: int
    rows: list[AltTextItemDTO]


class AltTextItemStatusRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: ItemStatus
