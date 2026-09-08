"""DTO двох тулів Content QA — Strategy Check і Framework Check (task-53).

Один рушій (`app/contentqa/**`), два тонкі REST-контракти: кожен приймає
сирі байти файлу (без multipart — той самий вибір, що й `tools_alt_text.py`)
і повертає готовий звіт, персистований у `content_qa_reports` (одна таблиця
на обидва тули, `doc_type` розрізняє — `ContentQaReport`/`app/db/models.py`).
Список/деталь дзеркалять `AltTextReportSummaryDTO`/`AltTextReportDTO`: summary
для списку минулих прогонів, повний DTO — для сторінки одного звіту (щоб
дати їй посилання, яке можна передати колезі).
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel

ContentQaDocType = Literal["strategy_xlsx", "framework_docx"]
ContentQaSeverity = Literal["error", "warn", "info"]
ContentQaKind = Literal["page", "blog"]
ContentQaIntent = Literal["commercial", "informational", "navigational"]
ContentQaLocale = Literal["en-US", "en-GB", "sv-SE"]
ContentQaSheetState = Literal["checked", "not_selected", "no_recognized_columns"]


class ContentQaProfileDTO(BaseModel):
    kind: ContentQaKind = "page"
    intent: ContentQaIntent = "commercial"
    locale: ContentQaLocale = "en-US"
    brand_names: list[str] = []
    # Аркуші, обрані оператором (task-87 §1.3) — `None` означає «усі
    # несучі», те саме поводження, що звіти до task-87.
    sheets: list[str] | None = None
    cross_sheet: bool = True


class ContentQaSheetDTO(BaseModel):
    name: str
    state: ContentQaSheetState
    item_count: int


class ContentQaSheetPreviewDTO(BaseModel):
    """Один аркуш у відповіді `/inspect` (task-87 §1.3, крок 1)."""

    name: str
    index: int
    load_bearing: bool
    columns: list[str]
    row_count: int
    blocks: int


class ContentQaInspectDTO(BaseModel):
    source_filename: str
    sheets: list[ContentQaSheetPreviewDTO]
    suggested_sheets: list[str]


class ContentQaFindingDTO(BaseModel):
    check: str
    severity: ContentQaSeverity
    ref: str
    detail: str
    # Фактичний текст поля, що спричинило finding (title/description/heading/…).
    # `None` — значення немає або показувати нічого (PLAN §12).
    value: str | None = None


class ContentQaReportSummaryDTO(BaseModel):
    id: str
    doc_type: ContentQaDocType
    client_id: str | None = None
    client_name: str | None = None
    source_filename: str
    item_count: int
    severity_counts: dict[str, int] = {}
    created_at: datetime


class ContentQaReportDTO(ContentQaReportSummaryDTO):
    profile: ContentQaProfileDTO
    skipped_sheets: list[str] = []
    findings: list[ContentQaFindingDTO] = []
    # `[]` для звітів до task-87 (колонка `NULL`) — той самий "не
    # додумуємо за старий звіт", що і скрізь у цьому DTO.
    sheets: list[ContentQaSheetDTO] = []


class ContentQaReportsPageDTO(BaseModel):
    total: int
    limit: int
    offset: int
    reports: list[ContentQaReportSummaryDTO]
