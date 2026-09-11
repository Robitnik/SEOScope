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
# task-91 §2.2: три стани клітинки/вердикту — той самий інваріант PLAN §12
# (`None` ≠ `""`), піднятий на рівень порівняльної таблиці.
MatrixCellState = Literal["value", "empty", "absent"]
MatrixVerdict = Literal["match", "mismatch", "not_comparable"]
MatrixRowKind = Literal["matched", "strategy_only", "framework_only"]
# task-92 §2.1: посегментна звірка Copy ↔ живий сайт — рядок таблиці
# "речення в Copy / речення на сайті / чи сходиться".
CopyRowState = Literal["match", "changed", "missing", "extra", "excluded"]
CopySegmentKind = Literal["heading", "sentence", "item", "directive"]
CopyConfidence = Literal["strong", "weak"]
CopyTabGenre = Literal["copy", "edit_brief"]
CopyTabRole = Literal["primary", "secondary"]
CopyTabState = Literal["checked", "no_url", "not_checked", "not_applicable"]


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


class CopyDiffPartDTO(BaseModel):
    """Один опкод послівного diff'у (task-92 §2.1). `tag="equal"` —
    спільні слова; `"changed"` — розійшлися (один бік може бути
    порожнім — слово лише додане чи лише прибране)."""

    tag: Literal["equal", "changed"]
    doc_text: str
    site_text: str


class CopyRowDTO(BaseModel):
    """Один рядок посегментної звірки Copy ↔ сайт — «речення в Copy /
    речення на сайті / чи сходиться» (task-92 §2.1). `ratio`/`confidence`
    — лише для `state="changed"` (вирішили ЗІСТАВЛЕННЯ в пару, не
    вердикт — task-92 §1.5: вердикт `match` завжди точна рівність після
    нормалізації, ratio 0.99 не рятує «8 AM» від «7 AM»)."""

    state: CopyRowState
    kind: CopySegmentKind
    section: str | None = None
    doc_text: str | None = None
    site_text: str | None = None
    ratio: float | None = None
    confidence: CopyConfidence | None = None
    diff_parts: list[CopyDiffPartDTO] = Field(default_factory=list)
    diff_summary: str | None = None
    note: str | None = None


class CopyStatsDTO(BaseModel):
    match: int = 0
    changed: int = 0
    missing: int = 0
    extra: int = 0
    excluded: int = 0
    coverage: float = 0.0


class CopyTabResultDTO(BaseModel):
    """Результат звірки ОДНІЄЇ Copy-вкладки з живою сторінкою (task-92
    §2.1). `role="secondary"` — мультимовний документ, вкладка з нижчим
    покриттям (task-92 §1.8/§2.5, LAXTON: English/Swedish Content на
    один документ, правильну видає ПОКРИТТЯ, не назва). `genre=
    "edit_brief"` — вкладка правок для розробника (маркери `[KEEP]`/
    `[EDIT]`), рядки `missing` там не є помилками копії."""

    tab_title: str
    genre: CopyTabGenre
    role: CopyTabRole
    state: CopyTabState
    reason: str | None = None
    language_hint: str | None = None
    rows: list[CopyRowDTO] = Field(default_factory=list)
    stats: CopyStatsDTO = Field(default_factory=CopyStatsDTO)


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
    # Лейн K (task-92): посегментна звірка кожної копі-вкладки — окрема
    # таблиця, не findings. `[]` на пакетах, прогнаних до task-92 (те саме
    # правило, що `matrix` на `ContentQaPackageDTO` для task-91: живе
    # ВСЕРЕДИНІ вже наявного `frameworks_json`, окрема колонка/міграція
    # не потрібна — цей блок і так персиститься по кожному фреймворку.
    copy_diffs: list[CopyTabResultDTO] = Field(default_factory=list)
    # URL сторінки з Framework-таблиці — те, проти чого фетчився сайт.
    # Раніше жив лише транзитно у `FrameworkOutcome.frame_item` і губився
    # при персистенції; Excel-аркуш Copy на сторінку (task-92, live
    # feedback) має показувати URL і посилання на документ у шапці.
    # `None` на пакетах до цього поля і на фреймворках без URL — не
    # вгадується з `matrix_json` заднім числом.
    page_url: str | None = None


class MatrixCellDTO(BaseModel):
    """Одна клітинка матриці (task-91 §2.2). `text` — скалярні поля (url/
    title/description/h1), `items` — множинні (keywords/links). Рівно один
    непорожній, коли `state == "value"`."""

    state: MatrixCellState
    text: str | None = None
    items: list[str] = Field(default_factory=list)


class MatrixSourceCellsDTO(BaseModel):
    url: MatrixCellDTO
    title: MatrixCellDTO
    description: MatrixCellDTO
    h1: MatrixCellDTO
    keywords: MatrixCellDTO
    links: MatrixCellDTO


class MatrixFieldVerdictDTO(BaseModel):
    state: MatrixVerdict
    detail: str | None = None
    # Which source(s) ("strategy"/"framework"/"site") are the odd one out
    # on a mismatch — `()` for match/not_comparable. Lets the UI/Excel
    # highlight only the drifting source red, not every source that has a
    # value (task-91 §3.6, live feedback).
    outlier_sources: list[str] = Field(default_factory=list)


class MatrixRowVerdictsDTO(BaseModel):
    url: MatrixFieldVerdictDTO
    title: MatrixFieldVerdictDTO
    description: MatrixFieldVerdictDTO
    h1: MatrixFieldVerdictDTO
    keywords: MatrixFieldVerdictDTO
    links: MatrixFieldVerdictDTO


class MatrixRowDTO(BaseModel):
    """Один рядок матриці = одна сторінка (task-91 §2.1). `strategy`/
    `framework`/`site` — `None`, коли та сторона рядка взагалі не існує
    (`kind="strategy_only"` не має `framework`/`site`, `kind=
    "framework_only"` не має `strategy`)."""

    key: str
    kind: MatrixRowKind
    label: str
    strategy: MatrixSourceCellsDTO | None = None
    framework: MatrixSourceCellsDTO | None = None
    site: MatrixSourceCellsDTO | None = None
    verdicts: MatrixRowVerdictsDTO
    # Google Doc фреймворка за цим рядком — для лінка в Excel `Comparison`
    # (live feedback). `None` для `strategy_only` і для матриць до цього поля.
    framework_document_id: str | None = None
    framework_document_name: str | None = None


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
    # `[]` на пакетах, прогнаних до task-91 (`matrix_json IS NULL` у БД) —
    # чесний "порівняльної таблиці ще нема", не вигаданий заднім числом.
    matrix: list[MatrixRowDTO] = Field(default_factory=list)


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
