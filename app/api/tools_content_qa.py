"""Strategy Check + Framework Check endpoints (task-53).

Один рушій (`app/contentqa/**`), два тонкі REST-ресурси в одному модулі, не
двох: 95% логіки (прийняти байти -> прочитати -> прогнати правила ->
персистувати -> серіалізувати) ідентичні, розходиться лише виклик ридера.

Персистовано в `content_qa_reports` (одна таблиця на обидва тули, `doc_type`
розрізняє) — щоб звіт пережив reload і мав власне посилання
(`#/tools/{strategy-check,framework-check}/<id>`), той самий принцип, що
`AltTextReport`/`RedirectCheck`.

Без `multipart/form-data`: файл один, тіло — сирі байти, ім'я — заголовком
`X-Filename` (той самий вибір, що `tools_alt_text.py`/`api/clients.py`).
"""

from __future__ import annotations

import collections
from collections.abc import Iterable
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from sqlalchemy.orm import Session

from app.api import register_router
from app.api.deps import PaginationDep, SessionDep
from app.contentqa.engine import run_checks
from app.contentqa.findings import Finding
from app.contentqa.model import ContentDoc, SheetInfo
from app.contentqa.readers.docx import read_framework_docx
from app.contentqa.readers.xlsx import (
    UnknownSheetError,
    inspect_strategy_xlsx,
    read_strategy_xlsx,
)
from app.contentqa.rules import Profile
from app.db.models import ContentQaReport
from app.db.repositories.clients import ClientRepository
from app.db.repositories.content_qa import ContentQaReportInput, ContentQaRepository
from app.schemas.api import ErrorDTO
from app.schemas.content_qa import (
    ContentQaFindingDTO,
    ContentQaInspectDTO,
    ContentQaIntent,
    ContentQaKind,
    ContentQaLocale,
    ContentQaProfileDTO,
    ContentQaReportDTO,
    ContentQaReportsPageDTO,
    ContentQaReportSummaryDTO,
    ContentQaSheetDTO,
    ContentQaSheetPreviewDTO,
)

router = APIRouter(prefix="/api/v1/tools", tags=["content-qa"])

MAX_UPLOAD_BYTES = 10 * 1024 * 1024


def _error(code: str, message: str) -> dict[str, object]:
    return ErrorDTO(error_code=code, message=message).model_dump()


async def _read_upload(request: Request, x_filename: str | None) -> tuple[str, bytes]:
    filename = (request.headers.get("x-filename") or x_filename or "upload").strip()[:255]
    content = await request.body()
    if not content:
        raise HTTPException(
            status_code=400, detail=_error("empty_upload", "The uploaded file is empty.")
        )
    if len(content) > MAX_UPLOAD_BYTES:
        mb = MAX_UPLOAD_BYTES // (1024 * 1024)
        raise HTTPException(
            status_code=413, detail=_error("upload_too_large", f"The file exceeds {mb} MB.")
        )
    return filename, content


def _build_profile(
    kind: ContentQaKind,
    intent: ContentQaIntent,
    locale: ContentQaLocale,
    brand: list[str] | None,
    *,
    cross_sheet: bool = True,
) -> Profile:
    names = tuple(b.strip() for b in (brand or []) if b.strip())
    return Profile(
        kind=kind, intent=intent, locale=locale, brand_names=names, cross_sheet=cross_sheet
    )


def _profile_dict(profile: Profile, *, sheets: list[str] | None = None) -> dict[str, object]:
    return {
        "kind": profile.kind,
        "intent": profile.intent,
        "locale": profile.locale,
        "brand_names": list(profile.brand_names),
        "sheets": sheets,
        "cross_sheet": profile.cross_sheet,
    }


def _sheet_state_dicts(sheet_states: tuple[SheetInfo, ...]) -> list[dict[str, object]]:
    return [
        {"name": s.name, "state": s.state, "item_count": s.item_count}
        for s in sheet_states
    ]


def _finding_dicts(findings: Iterable[Finding]) -> list[dict[str, str | None]]:
    return [f.to_dict() for f in findings]


def _summary_dto(report: ContentQaReport, *, client_name: str | None) -> ContentQaReportSummaryDTO:
    return ContentQaReportSummaryDTO(
        id=report.id,
        doc_type=report.doc_type,  # type: ignore[arg-type]
        client_id=report.client_id,
        client_name=client_name,
        source_filename=report.source_filename,
        item_count=report.item_count,
        severity_counts=report.severity_counts_json,
        created_at=report.created_at,
    )


def _detail_dto(report: ContentQaReport, *, client_name: str | None) -> ContentQaReportDTO:
    summary = _summary_dto(report, client_name=client_name)
    return ContentQaReportDTO(
        **summary.model_dump(),
        profile=ContentQaProfileDTO(**report.profile_json),
        skipped_sheets=report.skipped_sheets_json,
        findings=[ContentQaFindingDTO(**f) for f in report.findings_json],
        # `sheets_json IS NULL` для звітів до task-87 -> `[]`, не додумуємо.
        sheets=[ContentQaSheetDTO(**s) for s in (report.sheets_json or [])],
    )


def _client_name(sync_session: Session, client_id: str | None) -> str | None:
    if not client_id:
        return None
    client = ClientRepository(sync_session).get(client_id)
    return client.name if client else None


async def _run_and_persist(
    session: SessionDep,
    *,
    doc_type: str,
    doc: ContentDoc,
    filename: str,
    profile: Profile,
    client_id: str | None,
    sheets: list[str] | None = None,
) -> ContentQaReportDTO:
    findings = run_checks(doc, profile)

    def _persist(sync_session: Session) -> ContentQaReportDTO:
        client_name = _client_name(sync_session, client_id)
        report = ContentQaRepository(sync_session).create_report(ContentQaReportInput(
            client_id=client_id,
            doc_type=doc_type,
            source_filename=filename,
            profile=_profile_dict(profile, sheets=sheets),
            item_count=len(doc.items),
            skipped_sheets=doc.skipped_sheets,
            findings=_finding_dicts(findings),
            severity_counts=dict(collections.Counter(f.severity for f in findings)),
            sheets=_sheet_state_dicts(doc.sheet_states) if doc.sheet_states else None,
        ))
        return _detail_dto(report, client_name=client_name)

    dto = await session.run_sync(_persist)
    await session.commit()
    return dto


async def _list(
    session: SessionDep, *, doc_type: str, pagination: PaginationDep
) -> ContentQaReportsPageDTO:
    def _read(sync_session: Session) -> ContentQaReportsPageDTO:
        repository = ContentQaRepository(sync_session)
        reports, total = repository.list_reports(
            doc_type=doc_type, limit=pagination.limit, offset=pagination.offset
        )
        return ContentQaReportsPageDTO(
            total=total,
            limit=pagination.limit,
            offset=pagination.offset,
            reports=[
                _summary_dto(r, client_name=_client_name(sync_session, r.client_id))
                for r in reports
            ],
        )

    return await session.run_sync(_read)


async def _get(session: SessionDep, *, doc_type: str, report_id: str) -> ContentQaReportDTO:
    def _read(sync_session: Session) -> ContentQaReportDTO | None:
        report = ContentQaRepository(sync_session).get_report(report_id)
        if report is None or report.doc_type != doc_type:
            return None
        return _detail_dto(report, client_name=_client_name(sync_session, report.client_id))

    dto = await session.run_sync(_read)
    if dto is None:
        raise HTTPException(
            status_code=404,
            detail=_error("report_not_found", f"No report with id {report_id!r}."),
        )
    return dto


@router.post("/strategy-check/inspect", response_model=ContentQaInspectDTO)
async def inspect_strategy(
    request: Request,
    x_filename: str | None = None,
) -> ContentQaInspectDTO:
    """Крок 1 вибору аркуша (task-87 §1.3): сирі байти в, перелік аркушів із
    прев'ю назад — нічого не персистує, той самий stateless вибір, що
    /check. Фронт повторно надсилає ці ж байти в `/check` разом з обраними
    `sheet`; двократна передача ≤10 МБ дешевша за чернетковий upload-стор
    (`_StoredUpload`, TTL, прибирання), який тут не виправданий одним
    рішенням "які аркуші"."""
    filename, content = await _read_upload(request, x_filename)
    try:
        inspected = await run_in_threadpool(inspect_strategy_xlsx, content)
    except Exception as exc:  # openpyxl кидає різне на битих архівах
        raise HTTPException(
            status_code=400, detail=_error("unreadable_xlsx", str(exc))
        ) from exc

    return ContentQaInspectDTO(
        source_filename=filename,
        sheets=[
            ContentQaSheetPreviewDTO(
                name=s.name, index=s.index, load_bearing=s.load_bearing,
                columns=list(s.columns), row_count=s.row_count, blocks=s.blocks,
            )
            for s in inspected.sheets
        ],
        suggested_sheets=list(inspected.suggested_sheets),
    )


@router.post("/strategy-check/check", response_model=ContentQaReportDTO)
async def check_strategy(
    request: Request,
    session: SessionDep,
    x_filename: str | None = None,
    kind: ContentQaKind = "page",
    intent: ContentQaIntent = "commercial",
    locale: ContentQaLocale = "en-US",
    client_id: str | None = None,
    brand: Annotated[list[str] | None, Query()] = None,
    sheet: Annotated[list[str] | None, Query()] = None,
    cross_sheet: bool = True,
) -> ContentQaReportDTO:
    """SEO-стратегія (XLSX) -> findings, персистовано. Мапінг колонок і
    несучість аркуша визначаються самим ридером (§2.3 task-53 плану).

    `sheet` (0+ query-параметрів) обмежує перевірку обраними аркушами —
    `None`/відсутній перевіряє всі несучі аркуші, те саме поводження, що до
    task-87 (§1.3). `cross_sheet=False` рахує дублі/канібалізацію лише в
    межах кожного обраного аркуша окремо."""
    filename, content = await _read_upload(request, x_filename)
    try:
        doc = await run_in_threadpool(read_strategy_xlsx, content, source=filename, sheets=sheet)
    except UnknownSheetError as exc:
        raise HTTPException(
            status_code=400, detail=_error("unknown_sheet", str(exc))
        ) from exc
    except Exception as exc:  # openpyxl кидає різне на битих архівах
        raise HTTPException(
            status_code=400, detail=_error("unreadable_xlsx", str(exc))
        ) from exc

    profile = _build_profile(kind, intent, locale, brand, cross_sheet=cross_sheet)
    return await _run_and_persist(
        session, doc_type="strategy_xlsx", doc=doc, filename=filename,
        profile=profile, client_id=client_id, sheets=sheet,
    )


@router.get("/strategy-check/reports", response_model=ContentQaReportsPageDTO)
async def list_strategy_reports(
    session: SessionDep, pagination: PaginationDep
) -> ContentQaReportsPageDTO:
    return await _list(session, doc_type="strategy_xlsx", pagination=pagination)


@router.get(
    "/strategy-check/reports/{report_id}",
    response_model=ContentQaReportDTO,
    responses={404: {"model": ErrorDTO}},
)
async def get_strategy_report(report_id: str, session: SessionDep) -> ContentQaReportDTO:
    return await _get(session, doc_type="strategy_xlsx", report_id=report_id)


@router.post("/framework-check/check", response_model=ContentQaReportDTO)
async def check_framework(
    request: Request,
    session: SessionDep,
    x_filename: str | None = None,
    kind: ContentQaKind = "page",
    intent: ContentQaIntent = "commercial",
    locale: ContentQaLocale = "en-US",
    client_id: str | None = None,
    brand: Annotated[list[str] | None, Query()] = None,
) -> ContentQaReportDTO:
    """Page/blog-фреймворк (DOCX, без сусіднього framework_data.json) ->
    findings, персистовано.

    `kind` тут важливий по-справжньому (на відміну від strategy-check): FAQ-бенд
    (`FAQ_MIN`/`MAX`) діє лише для `blog` — `framework-rules.md` для сторінок
    прямо каже «no minimum or maximum» (перевірено на живому SOC-фреймворку).
    """
    filename, content = await _read_upload(request, x_filename)
    try:
        doc = await run_in_threadpool(read_framework_docx, content, source=filename)
    except Exception as exc:  # zipfile/ElementTree кидають різне на битих docx
        raise HTTPException(
            status_code=400, detail=_error("unreadable_docx", str(exc))
        ) from exc

    profile = _build_profile(kind, intent, locale, brand)
    return await _run_and_persist(
        session, doc_type="framework_docx", doc=doc, filename=filename,
        profile=profile, client_id=client_id,
    )


@router.get("/framework-check/reports", response_model=ContentQaReportsPageDTO)
async def list_framework_reports(
    session: SessionDep, pagination: PaginationDep
) -> ContentQaReportsPageDTO:
    return await _list(session, doc_type="framework_docx", pagination=pagination)


@router.get(
    "/framework-check/reports/{report_id}",
    response_model=ContentQaReportDTO,
    responses={404: {"model": ErrorDTO}},
)
async def get_framework_report(report_id: str, session: SessionDep) -> ContentQaReportDTO:
    return await _get(session, doc_type="framework_docx", report_id=report_id)


register_router(router)
