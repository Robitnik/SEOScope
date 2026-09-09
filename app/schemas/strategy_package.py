"""DTO для Strategy Package Check (task-90 §4.7/§5).

Один рушій (`app/contentqa/{package,crosscheck,readers/gdoc,readers/gsheet}.py`
+ `app/integrations/google_api/{drive,docs,sheets}_client.py`), персистовано
в `content_qa_packages` — **один рядок на весь пакет** (`ContentQaPackage`,
`app/db/models.py`): звіт пакета показує вкладки, зведені по всіх
фреймворках одразу, тож дочірні рядки на кожен документ не потрібні
(дизайн-рішення, задокументоване в моделі й міграції `b63ac07bcbc3`).

`ContentQaFindingDTO`/`ContentQaKind`/`ContentQaIntent`/`ContentQaLocale`
перевикористані з `app.schemas.content_qa` (task-53) — той самий фіндинг,
той самий профіль, лише інше джерело документів.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.content_qa import (
    ContentQaFindingDTO,
    ContentQaIntent,
    ContentQaKind,
    ContentQaLocale,
    ContentQaSheetDTO,
    ContentQaSheetPreviewDTO,
)

# "spreadsheet" — нативний Google Sheet; "xlsx_blob" — завантажений .xlsx на
# Диску; "document" — сам файл є Google Doc (не стратегія — помилка
# `unsupported_source`); "unsupported" — інший mimeType.
SourceKind = Literal["spreadsheet", "xlsx_blob", "document", "unsupported"]
FrameworkOrigin = Literal["strategy_link", "drive_search"]
FrameworkState = Literal["checked", "not_accessible", "no_recognized_tabs"]
CrossCheckState = Literal["checked", "not_checked", "not_applicable"]


class DriveFileDTO(BaseModel):
    id: str
    name: str
    mime_type: str
    web_view_link: str | None = None
    modified_time: str | None = None
    is_shared_drive: bool = False


class DriveFilesPageDTO(BaseModel):
    files: list[DriveFileDTO]
    next_page_token: str | None = None


class ResolveSourceDTO(BaseModel):
    """Крок 1 (task-90 §5): посилання/id -> файл + його вид. `kind` каже
    фронту, що показати далі — вибір аркуша (spreadsheet/xlsx_blob) чи
    помилку (document/unsupported: стратегія не може бути Google Doc)."""

    file: DriveFileDTO
    kind: SourceKind


class FrameworkCandidateDTO(BaseModel):
    """Один знайдений кандидат-фреймворк (task-90 §1.8/§5 крок 2) — з
    поміченим походженням, щоб оператор бачив, звідки він узятий."""

    document_id: str
    document_name: str | None = None
    mime_type: str | None = None
    origin: FrameworkOrigin
    # Присутнє лише для `origin="strategy_link"` — точний рядок стратегії.
    sheet: str | None = None
    row: int | None = None
    strategy_ref: str | None = None


class StrategyPackageInspectDTO(BaseModel):
    """Крок 2 (task-90 §5): прев'ю стратегії без побудови ContentDoc/
    findings — той самий stateless принцип, що `/strategy-check/inspect`."""

    source_name: str
    source_file_id: str
    sheets: list[ContentQaSheetPreviewDTO]
    suggested_sheets: list[str]
    candidates: list[FrameworkCandidateDTO]


class CrossCheckInfoDTO(BaseModel):
    name: str
    state: CrossCheckState
    reason: str | None = None


class FrameworkOutcomeDTO(BaseModel):
    """Один фреймворк у пакеті — повний результат (task-90 §2.5/§4.6):
    QA Frame + QA Copy + три звірки, усі findings разом, бо звіт пакета
    показує їх згруповано по лейну, не по документу."""

    source: FrameworkCandidateDTO
    state: FrameworkState
    reason: str | None = None
    frame_tab_title: str | None = None
    copy_tab_title: str | None = None
    findings: list[ContentQaFindingDTO] = Field(default_factory=list)
    frame_strategy_info: CrossCheckInfoDTO | None = None
    frame_site_info: CrossCheckInfoDTO | None = None
    copy_site_info: CrossCheckInfoDTO | None = None


class ContentQaPackageSummaryDTO(BaseModel):
    id: str
    client_id: str | None = None
    client_name: str | None = None
    source_kind: str
    source_name: str
    framework_count: int
    severity_counts: dict[str, int] = Field(default_factory=dict)
    created_at: datetime


class ContentQaPackageDTO(ContentQaPackageSummaryDTO):
    sheets: list[ContentQaSheetDTO] = Field(default_factory=list)
    strategy_findings: list[ContentQaFindingDTO] = Field(default_factory=list)
    aggregate_findings: list[ContentQaFindingDTO] = Field(default_factory=list)
    frameworks: list[FrameworkOutcomeDTO] = Field(default_factory=list)


class ContentQaPackagesPageDTO(BaseModel):
    total: int
    limit: int
    offset: int
    packages: list[ContentQaPackageSummaryDTO]


class ResolveSourceRequestDTO(BaseModel):
    url_or_id: str


class InspectPackageRequestDTO(BaseModel):
    file_id: str


class CheckPackageRequestDTO(BaseModel):
    file_id: str
    sheets: list[str] | None = None
    frameworks: list[FrameworkCandidateDTO] = Field(default_factory=list)
    cross_sheet: bool = True
    kind: ContentQaKind = "page"
    intent: ContentQaIntent = "commercial"
    locale: ContentQaLocale = "en-US"
    client_id: str | None = None
    brand: list[str] = Field(default_factory=list)
    check_site: bool = True
    check_published: bool = True
