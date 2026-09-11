"""Strategy Package Check ендпоінти (task-90 §4.7/§5).

Один рушій (`app/contentqa/{package,crosscheck}.py` + `readers/{gsheet,
gdoc}.py` + `app/integrations/google_api/{drive,docs,sheets}_client.py`),
персистовано в `content_qa_packages` — один рядок на весь пакет
(`app/db/models.py::ContentQaPackage`, дизайн-рішення в докстрінгу моделі).

Крок 1 (`/resolve`) + перегляд Диска (`/drive/files`) + крок 2 (`/inspect`)
+ крок 3 (`/check`) — той самий stateless принцип, що `/strategy-check/
inspect` → `/check` (task-87): нічого не персистується, доки оператор не
підтвердив обсяг. `run_package` (task-90 §2.6) — чиста функція без HTTP/
сесії БД, тож увесь звʼязок з FastAPI/Google API/БД живе тут, не в рушії.

`drive_not_configured` — окремий чесний HTTP-стан (503), не 500: якщо
`GOOGLE_API_REFRESH_TOKEN` не має `drive.readonly` (task-90 §4.0a), тул каже
це прямим текстом замість внутрішньої помилки.
"""

from __future__ import annotations

import collections
from dataclasses import asdict, dataclass
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.api import register_router
from app.api.deps import PaginationDep, SessionDep
from app.contentqa.crosscheck import CrossCheckInfo
from app.contentqa.findings import Finding
from app.contentqa.matrix import MatrixRow, build_matrix
from app.contentqa.matrix_xlsx import build_matrix_xlsx
from app.contentqa.model import ContentDoc
from app.contentqa.package import FrameworkOutcome, FrameworkSource, run_package
from app.contentqa.readers.gsheet import (
    find_framework_links,
    inspect_strategy_gsheet,
    read_strategy_gsheet,
)
from app.contentqa.readers.tabular import UnknownSheetError
from app.contentqa.readers.xlsx import inspect_strategy_xlsx, read_strategy_xlsx
from app.contentqa.rules import Profile
from app.db.models import ContentQaPackage
from app.db.repositories.clients import ClientRepository
from app.db.repositories.content_qa_packages import (
    ContentQaPackageInput,
    ContentQaPackageRepository,
)
from app.integrations.google_api import (
    GoogleApiConfigError,
    GoogleApiError,
    open_google_api_session,
)
from app.integrations.google_api.docs_client import DocsClient
from app.integrations.google_api.drive_client import (
    GOOGLE_DOC_MIME,
    GOOGLE_SHEET_MIME,
    XLSX_MIME,
    DriveClient,
    DriveFile,
    extract_file_id,
)
from app.integrations.google_api.sheets_client import SheetGrid, SheetsClient
from app.schemas.api import ErrorDTO
from app.schemas.content_qa import ContentQaFindingDTO, ContentQaSheetDTO, ContentQaSheetPreviewDTO
from app.schemas.strategy_package import (
    CheckPackageRequestDTO,
    ContentQaPackageDTO,
    ContentQaPackagesPageDTO,
    ContentQaPackageSummaryDTO,
    CrossCheckInfoDTO,
    DriveFileDTO,
    DriveFilesPageDTO,
    FrameworkCandidateDTO,
    FrameworkOutcomeDTO,
    InspectPackageRequestDTO,
    MatrixRowDTO,
    ResolveSourceDTO,
    ResolveSourceRequestDTO,
    StrategyPackageInspectDTO,
)

router = APIRouter(prefix="/api/v1/tools/strategy-package", tags=["content-qa"])

MAX_XLSX_BLOB_BYTES = 10 * 1024 * 1024
# Пошук по Диску за конвенцією `<CLIENT>_*FRAMEWORK*` (task-90 §1.8) — резерв,
# коли посилань зі стратегії немає/не всі. Стеля тримає прогін відповідальним
# навіть на широкому портфелі (3860 живих фреймворк-документів, task-90 §1.1).
MAX_DRIVE_SEARCH_RESULTS = 25


def _error(code: str, message: str) -> dict[str, object]:
    return ErrorDTO(error_code=code, message=message).model_dump()


@dataclass(frozen=True)
class _Clients:
    drive: DriveClient
    docs: DocsClient
    sheets: SheetsClient


def _open_clients() -> _Clients:
    """Одна сесія на весь запит — три клієнти поверх неї (task-90 §4.1),
    не три окремі відкриті `httpx.Client`."""
    try:
        session = open_google_api_session()
    except GoogleApiConfigError as exc:
        raise HTTPException(
            status_code=503,
            detail=_error(
                "drive_not_configured",
                "Google Drive access is not configured — see task-90 §4.0a "
                "(scripts/google_oauth_bootstrap.py needs the drive.readonly scope).",
            ),
        ) from exc
    return _Clients(
        drive=DriveClient(session), docs=DocsClient(session), sheets=SheetsClient(session)
    )


def _drive_file_dto(f: Any) -> DriveFileDTO:
    return DriveFileDTO(
        id=f.id, name=f.name, mime_type=f.mime_type,
        web_view_link=f.web_view_link, modified_time=f.modified_time,
        is_shared_drive=f.drive_id is not None,
    )


def _source_kind(mime_type: str) -> str:
    if mime_type == GOOGLE_SHEET_MIME:
        return "spreadsheet"
    if mime_type == XLSX_MIME:
        return "xlsx_blob"
    if mime_type == GOOGLE_DOC_MIME:
        return "document"
    return "unsupported"


def _client_prefix(source_name: str) -> str | None:
    """Перший `_`-токен імені файлу — конвенція клієнтського префікса
    (task-90 §1.8, живо заміряно: `SHB_SEO_STRATEGY_082626` -> фреймворки
    `SHB_FRAMEWORK_*`). `None`, якщо в імені взагалі немає `_`."""
    prefix, sep, _rest = source_name.partition("_")
    return prefix if sep and prefix else None


def _run_in_google_api_error(exc: GoogleApiError, *, code: str) -> HTTPException:
    return HTTPException(status_code=502, detail=_error(code, str(exc)))


@router.post("/resolve", response_model=ResolveSourceDTO)
async def resolve_source(body: ResolveSourceRequestDTO) -> ResolveSourceDTO:
    """Крок 1 (task-90 §5): вставлене посилання чи id -> файл + його вид."""
    file_id = extract_file_id(body.url_or_id)
    if file_id is None:
        raise HTTPException(
            status_code=400,
            detail=_error("invalid_drive_url", "Not a recognizable Google Drive URL or file id."),
        )
    clients = await run_in_threadpool(_open_clients)
    try:
        file = await run_in_threadpool(clients.drive.get_file, file_id)
    except GoogleApiError as exc:
        raise _run_in_google_api_error(exc, code="drive_error") from exc
    if file is None:
        about = await run_in_threadpool(clients.drive.about_user)
        share_hint = f" Share it with {about}." if about else ""
        raise HTTPException(
            status_code=404,
            detail=_error(
                "file_not_accessible",
                f"The agency account cannot see this file.{share_hint}",
            ),
        )
    return ResolveSourceDTO(file=_drive_file_dto(file), kind=_source_kind(file.mime_type))  # type: ignore[arg-type]


@router.get("/drive/files", response_model=DriveFilesPageDTO)
async def browse_drive(
    q: str = "",
    page_token: str | None = None,
    page_size: int = Query(default=25, ge=1, le=100),
) -> DriveFilesPageDTO:
    """Перегляд/пошук Диска (task-90 §5) — тільки стратегії й документи, не
    весь Диск: файли, з якими цей тул узагалі вміє щось робити."""
    mime_filter = (
        f"(mimeType='{GOOGLE_SHEET_MIME}' or mimeType='{XLSX_MIME}' "
        f"or mimeType='{GOOGLE_DOC_MIME}')"
    )
    query = f"{mime_filter} and trashed=false"
    if q.strip():
        safe = q.strip().replace("'", "\\'")
        query = f"name contains '{safe}' and {query}"
    clients = await run_in_threadpool(_open_clients)
    try:
        page = await run_in_threadpool(
            clients.drive.list_files, query=query, page_size=page_size, page_token=page_token
        )
    except GoogleApiError as exc:
        raise _run_in_google_api_error(exc, code="drive_error") from exc
    return DriveFilesPageDTO(
        files=[_drive_file_dto(f) for f in page.files], next_page_token=page.next_page_token
    )


def _read_strategy(
    clients: _Clients, file: DriveFile, mime_type: str
) -> tuple[tuple[SheetGrid, ...] | None, bytes | None]:
    if mime_type == GOOGLE_SHEET_MIME:
        grid = clients.sheets.get_grid(file.id)
        return grid, None
    if mime_type == XLSX_MIME:
        content = clients.drive.download_media(file.id)
        if len(content) > MAX_XLSX_BLOB_BYTES:
            mb = MAX_XLSX_BLOB_BYTES // (1024 * 1024)
            raise HTTPException(
                status_code=413, detail=_error("upload_too_large", f"The file exceeds {mb} MB.")
            )
        return None, content
    if mime_type == GOOGLE_DOC_MIME:
        raise HTTPException(
            status_code=400,
            detail=_error(
                "unsupported_source",
                "This file is a Google Doc, not a strategy spreadsheet — pick the "
                "SEO strategy sheet, not a framework.",
            ),
        )
    raise HTTPException(
        status_code=400,
        detail=_error("unsupported_source", f"Unsupported file type: {mime_type!r}."),
    )


def _strategy_doc(
    grid: tuple[SheetGrid, ...] | None,
    xlsx_content: bytes | None,
    *,
    source: str,
    sheets: list[str] | None,
) -> ContentDoc:
    if xlsx_content is not None:
        return read_strategy_xlsx(xlsx_content, source=source, sheets=sheets)
    assert grid is not None  # one of the two is always set — see `_read_strategy`
    return read_strategy_gsheet(grid, source=source, sheets=sheets)


@router.post("/inspect", response_model=StrategyPackageInspectDTO)
async def inspect_package(body: InspectPackageRequestDTO) -> StrategyPackageInspectDTO:
    """Крок 2 (task-90 §5): аркуші стратегії + знайдені фреймворки —
    посилання зі стратегії (§1.8, основне джерело) плюс резервний пошук по
    Диску за префіксом клієнта. Нічого не персистується."""
    clients = await run_in_threadpool(_open_clients)
    try:
        file = await run_in_threadpool(clients.drive.get_file, body.file_id)
    except GoogleApiError as exc:
        raise _run_in_google_api_error(exc, code="drive_error") from exc
    if file is None:
        raise HTTPException(
            status_code=404, detail=_error("file_not_accessible", "File not accessible.")
        )

    def _scan() -> tuple[Any, Any, list[FrameworkCandidateDTO]]:
        grid, xlsx_content = _read_strategy(clients, file, file.mime_type)
        if grid is not None:
            inspection = inspect_strategy_gsheet(grid)
            link_candidates = find_framework_links(grid)
        else:
            assert xlsx_content is not None  # one of the two is always set
            inspection = inspect_strategy_xlsx(xlsx_content)
            link_candidates = ()

        candidates: list[FrameworkCandidateDTO] = []
        seen_ids: set[str] = set()
        for c in link_candidates:
            if c.document_id in seen_ids:
                continue
            seen_ids.add(c.document_id)
            candidates.append(FrameworkCandidateDTO(
                document_id=c.document_id, document_name=c.display_text,
                origin="strategy_link", sheet=c.sheet, row=c.row,
                strategy_ref=f"{c.sheet}!row {c.row}",
            ))

        prefix = _client_prefix(file.name)
        if prefix:
            found = clients.drive.list_files(
                query=(
                    f"name contains '{prefix}_' and name contains 'FRAMEWORK' "
                    f"and mimeType='{GOOGLE_DOC_MIME}' and trashed=false"
                ),
                page_size=MAX_DRIVE_SEARCH_RESULTS,
            )
            for f in found.files:
                if f.id in seen_ids or "_CHECK_" in f.name.upper():
                    continue
                seen_ids.add(f.id)
                candidates.append(FrameworkCandidateDTO(
                    document_id=f.id, document_name=f.name, mime_type=f.mime_type,
                    origin="drive_search",
                ))
        return inspection, None, candidates

    try:
        inspection, _unused, candidates = await run_in_threadpool(_scan)
    except UnknownSheetError as exc:
        raise HTTPException(status_code=400, detail=_error("unknown_sheet", str(exc))) from exc
    except GoogleApiError as exc:
        raise _run_in_google_api_error(exc, code="drive_error") from exc

    return StrategyPackageInspectDTO(
        source_name=file.name, source_file_id=file.id,
        sheets=[
            ContentQaSheetPreviewDTO(
                name=s.name, index=s.index, load_bearing=s.load_bearing,
                columns=list(s.columns), row_count=s.row_count, blocks=s.blocks,
            )
            for s in inspection.sheets
        ],
        suggested_sheets=list(inspection.suggested_sheets),
        candidates=candidates,
    )


def _build_profile(body: CheckPackageRequestDTO) -> Profile:
    names = tuple(b.strip() for b in body.brand if b.strip())
    return Profile(
        kind=body.kind, intent=body.intent, locale=body.locale,
        brand_names=names, cross_sheet=body.cross_sheet,
    )


def _crosscheck_info_dto(info: CrossCheckInfo | None) -> CrossCheckInfoDTO | None:
    if info is None:
        return None
    return CrossCheckInfoDTO(name=info.name, state=info.state, reason=info.reason)


def _crosscheck_info_dict(info: CrossCheckInfo | None) -> dict[str, object] | None:
    dto = _crosscheck_info_dto(info)
    return dto.model_dump() if dto is not None else None


def _finding_dicts(findings: list[Finding]) -> list[dict[str, str | None]]:
    return [f.to_dict() for f in findings]


def _framework_outcome_dict(outcome: FrameworkOutcome) -> dict[str, Any]:
    source = outcome.source
    return {
        "source": {
            "document_id": source.document_id, "document_name": source.document_name,
            "origin": source.origin, "strategy_ref": source.strategy_ref,
        },
        "state": outcome.state,
        "reason": outcome.reason,
        "frame_tab_title": outcome.frame_tab_title,
        "copy_tab_title": outcome.copy_tab_title,
        "findings": _finding_dicts(outcome.findings),
        "frame_strategy_info": _crosscheck_info_dict(outcome.frame_strategy_info),
        "frame_site_info": _crosscheck_info_dict(outcome.frame_site_info),
        "copy_site_info": _crosscheck_info_dict(outcome.copy_site_info),
        # Лейн K (task-92) — `CopyTabResult`'s fields already match
        # `CopyTabResultDTO` field-for-field (same trick as
        # `_matrix_row_dicts`), so `asdict()` needs no hand-written
        # mapping. Lives inline in `frameworks_json`, not a new column —
        # this blob is already persisted per-framework.
        "copy_diffs": [asdict(cd) for cd in outcome.copy_diffs],
        # The URL the site was fetched against — `frame_item` itself stays
        # transient (task-91 decision), but the Copy per-page Excel sheets
        # need the URL in their header long after the run (live feedback).
        "page_url": outcome.frame_item.url if outcome.frame_item is not None else None,
    }


def _sheet_state_dicts(sheet_states: tuple[Any, ...]) -> list[dict[str, object]]:
    return [{"name": s.name, "state": s.state, "item_count": s.item_count} for s in sheet_states]


def _matrix_row_dicts(rows: list[MatrixRow]) -> list[dict[str, Any]]:
    """`MatrixRow`/`Cell`/`SourceCells`/`FieldVerdict`/`RowVerdicts` are
    plain nested dataclasses whose field names already match
    `MatrixRowDTO` field-for-field (task-91 §2.2) — `asdict()` gives exact
    JSON-storable dicts without a second hand-written mapping to drift."""
    return [asdict(row) for row in rows]


def _client_name(sync_session: Session, client_id: str | None) -> str | None:
    if not client_id:
        return None
    client = ClientRepository(sync_session).get(client_id)
    return client.name if client else None


def _package_summary_dto(
    package: ContentQaPackage, *, client_name: str | None
) -> ContentQaPackageSummaryDTO:
    return ContentQaPackageSummaryDTO(
        id=package.id, client_id=package.client_id, client_name=client_name,
        source_kind=package.source_kind, source_name=package.source_name,
        framework_count=package.framework_count,
        severity_counts=package.severity_counts_json, created_at=package.created_at,
    )


def _package_detail_dto(
    package: ContentQaPackage, *, client_name: str | None
) -> ContentQaPackageDTO:
    summary = _package_summary_dto(package, client_name=client_name)
    return ContentQaPackageDTO(
        **summary.model_dump(),
        strategy_findings=[ContentQaFindingDTO(**f) for f in package.strategy_findings_json],
        aggregate_findings=[ContentQaFindingDTO(**f) for f in package.aggregate_findings_json],
        frameworks=[FrameworkOutcomeDTO(**f) for f in package.frameworks_json],
        sheets=[ContentQaSheetDTO(**s) for s in package.sheets_json],
        # `matrix_json IS NULL` для пакетів до task-91 -> `[]`, не додумуємо.
        matrix=[MatrixRowDTO(**m) for m in (package.matrix_json or [])],
    )


@router.post("/check", response_model=ContentQaPackageDTO)
async def check_package(body: CheckPackageRequestDTO, session: SessionDep) -> ContentQaPackageDTO:
    """Крок 3 (task-90 §5): повний прогін — QA стратегії + всі обрані
    фреймворки (кожен: Frame QA + Copy QA + три звірки), персистовано в
    один рядок `content_qa_packages`."""
    clients = await run_in_threadpool(_open_clients)
    try:
        file = await run_in_threadpool(clients.drive.get_file, body.file_id)
    except GoogleApiError as exc:
        raise _run_in_google_api_error(exc, code="drive_error") from exc
    if file is None:
        raise HTTPException(
            status_code=404, detail=_error("file_not_accessible", "File not accessible.")
        )

    def _build_strategy() -> ContentDoc:
        grid, xlsx_content = _read_strategy(clients, file, file.mime_type)
        return _strategy_doc(grid, xlsx_content, source=file.name, sheets=body.sheets)

    try:
        strategy = await run_in_threadpool(_build_strategy)
    except UnknownSheetError as exc:
        raise HTTPException(status_code=400, detail=_error("unknown_sheet", str(exc))) from exc
    except GoogleApiError as exc:
        raise _run_in_google_api_error(exc, code="drive_error") from exc

    sources = [
        FrameworkSource(
            document_id=c.document_id, document_name=c.document_name,
            origin=c.origin, strategy_ref=c.strategy_ref,
        )
        for c in body.frameworks
    ]
    profile = _build_profile(body)

    def _run() -> tuple[Any, list[MatrixRow]]:
        result = run_package(
            strategy, sources, docs_client=clients.docs, profile=profile,
            check_site=body.check_site, check_published=body.check_published,
        )
        # Чиста функція над тим, що run_package уже прочитав (task-91
        # §2.2) — рахується в тому самому потоці, окремого I/O нема.
        return result, build_matrix(strategy, result.frameworks)

    result, matrix_rows = await run_in_threadpool(_run)

    def _persist(sync_session: Session) -> ContentQaPackageDTO:
        client_name = _client_name(sync_session, body.client_id)
        all_severities = (
            [f.severity for f in result.strategy_findings]
            + [f.severity for f in result.aggregate_findings]
            + [f.severity for o in result.frameworks for f in o.findings]
        )
        package = ContentQaPackageRepository(sync_session).create_package(ContentQaPackageInput(
            client_id=body.client_id,
            source_kind=_source_kind(file.mime_type),
            source_file_id=file.id,
            source_name=file.name,
            profile=body.model_dump(
                include={"kind", "intent", "locale", "brand", "cross_sheet", "sheets"}
            ),
            framework_count=len(sources),
            strategy_findings=_finding_dicts(result.strategy_findings),
            aggregate_findings=_finding_dicts(result.aggregate_findings),
            frameworks=[_framework_outcome_dict(o) for o in result.frameworks],
            severity_counts=dict(collections.Counter(all_severities)),
            sheets=_sheet_state_dicts(strategy.sheet_states),
            matrix=_matrix_row_dicts(matrix_rows),
        ))
        return _package_detail_dto(package, client_name=client_name)

    dto = await session.run_sync(_persist)
    await session.commit()
    return dto


@router.get("/reports", response_model=ContentQaPackagesPageDTO)
async def list_packages(session: SessionDep, pagination: PaginationDep) -> ContentQaPackagesPageDTO:
    def _read(sync_session: Session) -> ContentQaPackagesPageDTO:
        repository = ContentQaPackageRepository(sync_session)
        packages, total = repository.list_packages(limit=pagination.limit, offset=pagination.offset)
        return ContentQaPackagesPageDTO(
            total=total, limit=pagination.limit, offset=pagination.offset,
            packages=[
                _package_summary_dto(p, client_name=_client_name(sync_session, p.client_id))
                for p in packages
            ],
        )

    return await session.run_sync(_read)


@router.get(
    "/reports/{package_id}",
    response_model=ContentQaPackageDTO,
    responses={404: {"model": ErrorDTO}},
)
async def get_package(package_id: str, session: SessionDep) -> ContentQaPackageDTO:
    def _read(sync_session: Session) -> ContentQaPackageDTO | None:
        package = ContentQaPackageRepository(sync_session).get_package(package_id)
        if package is None:
            return None
        client_name = _client_name(sync_session, package.client_id)
        return _package_detail_dto(package, client_name=client_name)

    dto = await session.run_sync(_read)
    if dto is None:
        raise HTTPException(
            status_code=404,
            detail=_error("package_not_found", f"No package with id {package_id!r}."),
        )
    return dto


@router.get("/reports/{package_id}/matrix.xlsx", responses={404: {"model": ErrorDTO}})
async def download_package_matrix_xlsx(package_id: str, session: SessionDep) -> StreamingResponse:
    """Excel-експорт порівняльної матриці (task-91 §3.5) + аркуш Copy
    (task-92 §2.6) — читає вже ПЕРСИСТОВАНУ матрицю з `content_qa_
    packages.matrix_json` і `copy_diffs` з уже персистованого
    `frameworks_json`, нічого не перераховує заново."""

    def _read(sync_session: Session) -> ContentQaPackage | None:
        return ContentQaPackageRepository(sync_session).get_package(package_id)

    package = await session.run_sync(_read)
    if package is None:
        raise HTTPException(
            status_code=404,
            detail=_error("package_not_found", f"No package with id {package_id!r}."),
        )
    matrix_rows = [MatrixRowDTO(**m) for m in (package.matrix_json or [])]
    frameworks = [FrameworkOutcomeDTO(**f) for f in package.frameworks_json]
    data = build_matrix_xlsx(matrix_rows, frameworks=frameworks)
    safe_name = "".join(c if c.isalnum() else "_" for c in package.source_name).strip("_")
    filename = f"{safe_name or package.id}_Comparison_Matrix.xlsx"
    return StreamingResponse(
        iter([data]),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


register_router(router)
