"""Image Alt Text tool endpoints (task-27).

Свідомо **не** через job-рушій: парсинг таблиці — мілісекунди, перевірка
сторінки — один HTTP-запит, тож `SyncJob`/SSE/lease тут дали б лише
складність (див. таск-док, «Архітектура»). Блокуючі частини (openpyxl,
`urllib`) ідуть через `run_in_threadpool`, як і експорти в `api/blogs.py`.

Двокроковий upload (`/uploads` → `/reports`) — вимушений: вкладку XLSX і
мапінг колонок неможливо знати до читання файлу.

Кожен мутуючий ендпоінт комітить сам: `app.db.session.get_db` віддає сесію
без auto-commit, тож запис, який лише flush-нувся, інакше тихо відкотився б
(той самий коментар — у `api/clients.py`).
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import time
import unicodedata
import uuid
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse
from openpyxl import Workbook
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.api import register_router
from app.api.deps import PaginationDep, SessionDep, SettingsDep
from app.core.config import Settings
from app.core.security import PathTraversalError, safe_resolve
from app.db.models import AltTextItem, AltTextReport
from app.db.repositories.alt_text import AltTextItemInput, AltTextRepository, ItemFilters
from app.db.repositories.clients import ClientRepository
from app.db.repositories.monitor import MonitorRepository
from app.schemas.api import ErrorDTO
from app.schemas.tools import (
    AltTextItemDTO,
    AltTextItemsPageDTO,
    AltTextItemStatusRequest,
    AltTextMediaBatchDTO,
    AltTextMediaBatchRequest,
    AltTextReportCreateRequest,
    AltTextReportDTO,
    AltTextReportSummaryDTO,
    AltTextUploadDTO,
    MediaCandidateDTO,
    SheetPreviewDTO,
)
from app.tools.alt_text import wp_rest
from app.tools.alt_text.tables import (
    MAX_PAGE_URLS,
    REQUIRED_FIELDS,
    SourceError,
    detect_source_kind,
    inspect_source,
    parse_rows,
)
from app.tools.alt_text.verify import VerifyResult, VerifyStatus, verify_alt
from app.tools.alt_text.wp_media import derive_search_key, derive_upload_location, is_searchable

router = APIRouter(prefix="/api/v1/tools/alt-text", tags=["tools"])

MAX_UPLOAD_BYTES = 10 * 1024 * 1024
UPLOAD_DIR = "alt-text-uploads"
# Завантажений файл потрібен лише між кроком 1 і кроком 2; усе, що пережило
# цей час, — покинутий чернетковий upload.
UPLOAD_TTL_SECONDS = 6 * 60 * 60
# Скільки сторінок обходить одна перевірка. Кожна — реальний запит до сайту
# клієнта, тож стеля свідомо низька; решта сторінок лишається видимою в UI.
MAX_VERIFY_PAGES = 3

_REPORT_NOT_FOUND = ErrorDTO(
    error_code="report_not_found", message="Report not found"
).model_dump()
_ITEM_NOT_FOUND = ErrorDTO(error_code="item_not_found", message="Row not found").model_dump()
_UPLOAD_NOT_FOUND = ErrorDTO(
    error_code="upload_not_found",
    message="This upload has expired. Upload the file again.",
).model_dump()


def _error(code: str, message: str, **details: Any) -> dict[str, Any]:
    return ErrorDTO(error_code=code, message=message, details=details).model_dump()


# --- сховище чернеткових завантажень -------------------------------------------


@dataclass(frozen=True)
class _StoredUpload:
    filename: str
    sha256: str
    content: bytes


def _upload_paths(settings: Settings, upload_id: str) -> tuple[Path, Path]:
    # Імʼя генеруємо самі (`uuid4`), ніколи не з імені користувача — тож після
    # `safe_resolve` санітизувати вже нічого (PLAN §13).
    base = safe_resolve(settings.artifacts_dir, f"{UPLOAD_DIR}/{upload_id}")
    return base.with_suffix(".bin"), base.with_suffix(".json")


def _purge_stale_uploads(settings: Settings) -> None:
    directory = settings.artifacts_dir / UPLOAD_DIR
    if not directory.is_dir():
        return
    cutoff = time.time() - UPLOAD_TTL_SECONDS
    for path in directory.iterdir():
        try:
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink(missing_ok=True)
        except OSError:
            # Прибирання не має ламати запит оператора.
            continue


def _store_upload(settings: Settings, filename: str, content: bytes) -> str:
    upload_id = str(uuid.uuid4())
    data_path, meta_path = _upload_paths(settings, upload_id)
    data_path.parent.mkdir(parents=True, exist_ok=True)
    data_path.write_bytes(content)
    meta_path.write_text(
        json.dumps({"filename": filename, "sha256": hashlib.sha256(content).hexdigest()}),
        encoding="utf-8",
    )
    return upload_id


def _load_upload(settings: Settings, upload_id: str) -> _StoredUpload | None:
    try:
        data_path, meta_path = _upload_paths(settings, upload_id)
    except PathTraversalError:
        return None
    if not data_path.is_file() or not meta_path.is_file():
        return None
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    return _StoredUpload(
        filename=str(meta.get("filename") or "upload"),
        sha256=str(meta.get("sha256") or ""),
        content=data_path.read_bytes(),
    )


def _drop_upload(settings: Settings, upload_id: str) -> None:
    try:
        data_path, meta_path = _upload_paths(settings, upload_id)
    except PathTraversalError:
        return
    data_path.unlink(missing_ok=True)
    meta_path.unlink(missing_ok=True)


# --- DTO ----------------------------------------------------------------------


def _item_dto(item: AltTextItem) -> AltTextItemDTO:
    derivation = item.derivation_json or {}
    media = item.wp_media_json or {}
    return AltTextItemDTO(
        id=item.id,
        row_index=item.row_index,
        image_url=item.image_url,
        search_key=item.search_key,
        search_key_fallback=item.search_key_fallback,
        url_kind=item.url_kind,  # type: ignore[arg-type]
        stripped=list(derivation.get("stripped") or []),
        derivation_warnings=list(derivation.get("warnings") or []),
        searchable=is_searchable(item.url_kind, item.search_key),
        new_alt=item.new_alt,
        old_alt=item.old_alt,
        page_url=item.page_url,
        page_urls=list(item.page_urls_json or ([item.page_url] if item.page_url else [])),
        extra=dict(item.extra_json or {}),
        status=item.status,  # type: ignore[arg-type]
        status_changed_at=item.status_changed_at,
        verify_status=item.verify_status,  # type: ignore[arg-type]
        verify_alt=item.verify_alt,
        verify_http_status=item.verify_http_status,
        verify_detail=item.verify_detail,
        verify_checked_at=item.verify_checked_at,
        wp_media_id=item.wp_media_id,
        wp_media_status=item.wp_media_status,  # type: ignore[arg-type]
        wp_media_confidence=item.wp_media_confidence,  # type: ignore[arg-type]
        wp_media_alt=item.wp_media_alt,
        wp_media_admin_url=item.wp_media_admin_url,
        wp_media_detail=item.wp_media_detail,
        wp_media_candidates=[
            MediaCandidateDTO(**candidate) for candidate in (media.get("candidates") or [])
        ],
        wp_media_checked_at=item.wp_media_checked_at,
    )


def _summary_dto(
    report: AltTextReport, *, status_counts: dict[str, int], client_name: str | None
) -> AltTextReportSummaryDTO:
    return AltTextReportSummaryDTO(
        id=report.id,
        title=report.title,
        client_id=report.client_id,
        client_name=client_name,
        source_filename=report.source_filename,
        source_kind=report.source_kind,  # type: ignore[arg-type]
        source_sheet=report.source_sheet,
        row_count=report.row_count,
        skipped_count=report.skipped_count,
        status_counts=status_counts,
        created_at=report.created_at,
        updated_at=report.updated_at,
    )


def _report_dto(session: Session, report: AltTextReport) -> AltTextReportDTO:
    repository = AltTextRepository(session)
    client_name = None
    if report.client_id:
        client = ClientRepository(session).get(report.client_id)
        client_name = client.name if client else None
    summary = _summary_dto(
        report,
        status_counts=repository.status_counts(report.id),
        client_name=client_name,
    )
    return AltTextReportDTO(
        **summary.model_dump(),
        column_map=dict(report.column_map_json or {}),
        warnings=list(report.warnings_json or []),
        verify_counts=repository.verify_counts(report.id),
        verifiable_count=repository.verifiable_count(report.id),
    )


# --- крок 1: завантаження й інспекція -----------------------------------------


@router.post("/uploads", response_model=AltTextUploadDTO)
async def upload_source(
    request: Request, settings: SettingsDep, x_filename: str | None = None
) -> AltTextUploadDTO:
    """Прийняти таблицю аудиту як сирі байти тіла запиту.

    Без `multipart/form-data`: файл один, інших полів у формі немає, тож
    multipart нічого не дає, зате вимагав би залежності `python-multipart`
    (той самий вибір, що й `upload_logo` в `api/clients.py`). Імʼя файлу —
    заголовком `X-Filename`, воно потрібне лише для показу й підказки формату.
    """
    filename = (request.headers.get("x-filename") or x_filename or "upload").strip()[:255]
    content = await request.body()
    if not content:
        raise HTTPException(
            status_code=400, detail=_error("empty_upload", "The uploaded file is empty.")
        )
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=_error(
                "upload_too_large",
                f"The file exceeds {MAX_UPLOAD_BYTES // (1024 * 1024)} MB.",
            ),
        )

    try:
        inspected = await run_in_threadpool(inspect_source, filename, content)
    except SourceError as exc:
        status = 415 if exc.code == "unsupported_source" else 400
        raise HTTPException(status_code=status, detail=_error(exc.code, str(exc))) from exc

    await run_in_threadpool(_purge_stale_uploads, settings)
    upload_id = await run_in_threadpool(_store_upload, settings, filename, content)
    return AltTextUploadDTO(
        upload_id=upload_id,
        source_filename=filename,
        source_kind=inspected.kind.value,
        sheets=[
            SheetPreviewDTO(
                name=sheet.name,
                headers=list(sheet.headers),
                sample_rows=[list(row) for row in sheet.sample_rows],
                header_row_index=sheet.header_row_index,
                suggested_column_map=sheet.suggested_column_map,
                warnings=list(sheet.warnings),
            )
            for sheet in inspected.sheets
        ],
        default_sheet=inspected.default_sheet,
        delimiter=inspected.delimiter,
        encoding=inspected.encoding,
    )


# --- крок 2: створення звіту ---------------------------------------------------


@router.post(
    "/reports",
    response_model=AltTextReportDTO,
    status_code=201,
    responses={404: {"model": ErrorDTO}, 409: {"model": ErrorDTO}, 422: {"model": ErrorDTO}},
)
async def create_report(
    body: AltTextReportCreateRequest,
    session: SessionDep,
    settings: SettingsDep,
    force: bool = Query(default=False),
) -> AltTextReportDTO:
    upload = await run_in_threadpool(_load_upload, settings, body.upload_id)
    if upload is None:
        raise HTTPException(status_code=404, detail=_UPLOAD_NOT_FOUND)

    column_map = {str(name): index for name, index in body.column_map.items()}
    missing = [name for name in REQUIRED_FIELDS if name not in column_map]
    if missing:
        raise HTTPException(
            status_code=422,
            detail=_error(
                "invalid_column_map",
                f"Map a column for: {', '.join(missing)}.",
                missing=missing,
            ),
        )

    site_host = None
    if body.client_id is not None:
        client = await session.run_sync(lambda s: ClientRepository(s).get(body.client_id or ""))
        if client is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorDTO(
                    error_code="client_not_found", message="Client not found"
                ).model_dump(),
            )
        site_host = client.domain

    existing = await session.run_sync(
        lambda s: AltTextRepository(s).find_by_source(upload.sha256, body.sheet)
    )
    if existing is not None and not force:
        raise HTTPException(
            status_code=409,
            detail=_error(
                "report_exists",
                "This file and sheet were already imported. "
                "Open the existing report, or send force=true to import it again.",
                existing_report_id=existing.id,
                existing_title=existing.title,
            ),
        )

    try:
        parsed = await run_in_threadpool(
            parse_rows,
            upload.filename,
            upload.content,
            sheet=body.sheet,
            column_map=column_map,
        )
    except SourceError as exc:
        status = 415 if exc.code == "unsupported_source" else 422
        raise HTTPException(status_code=status, detail=_error(exc.code, str(exc))) from exc

    items = []
    for row in parsed.rows:
        derived = derive_search_key(row.image_url, site_host=site_host)
        items.append(
            AltTextItemInput(
                row_index=row.row_index,
                image_url=row.image_url[:2048],
                search_key=derived.search_key,
                search_key_fallback=derived.search_key_fallback,
                url_kind=derived.url_kind.value,
                derivation={
                    "stripped": list(derived.stripped),
                    "warnings": list(derived.warnings),
                    "filename": derived.filename,
                    "extension": derived.extension,
                },
                new_alt=row.new_alt,
                old_alt=row.old_alt,
                page_urls=[url[:2048] for url in row.page_urls[:MAX_PAGE_URLS]],
                extra=row.extra,
            )
        )

    source_kind = detect_source_kind(upload.filename, upload.content).value

    def _persist(sync_session: Session) -> AltTextReportDTO:
        report = AltTextRepository(sync_session).create_report(
            client_id=body.client_id,
            title=body.title.strip(),
            source_filename=upload.filename,
            source_kind=source_kind,
            source_sheet=body.sheet,
            source_sha256=upload.sha256,
            column_map=column_map,
            skipped_count=parsed.skipped_count,
            warnings=parsed.warnings,
            items=items,
        )
        return _report_dto(sync_session, report)

    dto = await session.run_sync(_persist)
    await session.commit()
    # Файл більше не потрібен: усе, що з нього треба, вже в БД.
    await run_in_threadpool(_drop_upload, settings, body.upload_id)
    return dto


# --- читання -------------------------------------------------------------------


@router.get("/reports", response_model=list[AltTextReportSummaryDTO])
async def list_reports(
    session: SessionDep, pagination: PaginationDep
) -> list[AltTextReportSummaryDTO]:
    def _read(sync_session: Session) -> list[AltTextReportSummaryDTO]:
        repository = AltTextRepository(sync_session)
        reports, _total = repository.list_reports(
            limit=pagination.limit, offset=pagination.offset
        )
        clients = ClientRepository(sync_session)
        summaries = []
        for report in reports:
            client_name = None
            if report.client_id:
                client = clients.get(report.client_id)
                client_name = client.name if client else None
            summaries.append(
                _summary_dto(
                    report,
                    status_counts=repository.status_counts(report.id),
                    client_name=client_name,
                )
            )
        return summaries

    return await session.run_sync(_read)


@router.get(
    "/reports/{report_id}",
    response_model=AltTextReportDTO,
    responses={404: {"model": ErrorDTO}},
)
async def get_report(report_id: str, session: SessionDep) -> AltTextReportDTO:
    def _read(sync_session: Session) -> AltTextReportDTO | None:
        report = AltTextRepository(sync_session).get_report(report_id)
        return None if report is None else _report_dto(sync_session, report)

    dto = await session.run_sync(_read)
    if dto is None:
        raise HTTPException(status_code=404, detail=_REPORT_NOT_FOUND)
    return dto


@router.get(
    "/reports/{report_id}/items",
    response_model=AltTextItemsPageDTO,
    responses={404: {"model": ErrorDTO}},
)
async def list_items(
    report_id: str,
    session: SessionDep,
    q: str | None = None,
    status: str | None = None,
    url_kind: str | None = None,
    verify: str | None = None,
    has_page: bool | None = None,
    media_status: str | None = None,
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> AltTextItemsPageDTO:
    filters = ItemFilters(
        query=q,
        status=status,
        url_kind=url_kind,
        verify_status=verify,
        has_page=has_page,
        media_status=media_status,
    )

    def _read(sync_session: Session) -> AltTextItemsPageDTO | None:
        repository = AltTextRepository(sync_session)
        if repository.get_report(report_id) is None:
            return None
        rows, total = repository.list_items(
            report_id, filters=filters, limit=limit, offset=offset
        )
        return AltTextItemsPageDTO(
            report_id=report_id,
            total=total,
            limit=limit,
            offset=offset,
            rows=[_item_dto(row) for row in rows],
        )

    page = await session.run_sync(_read)
    if page is None:
        raise HTTPException(status_code=404, detail=_REPORT_NOT_FOUND)
    return page


# --- прогрес -------------------------------------------------------------------


@router.patch(
    "/reports/{report_id}/items/{item_id}",
    response_model=AltTextItemDTO,
    responses={404: {"model": ErrorDTO}},
)
async def set_item_status(
    report_id: str, item_id: str, body: AltTextItemStatusRequest, session: SessionDep
) -> AltTextItemDTO:
    """Позначити прогрес рядка. Ідемпотентно: той самий статус — той самий результат."""

    def _write(sync_session: Session) -> AltTextItemDTO | None:
        repository = AltTextRepository(sync_session)
        item = repository.get_item(report_id, item_id)
        if item is None:
            return None
        repository.set_status(item, body.status)
        return _item_dto(item)

    dto = await session.run_sync(_write)
    if dto is None:
        raise HTTPException(status_code=404, detail=_ITEM_NOT_FOUND)
    await session.commit()
    return dto


# --- перевірка -----------------------------------------------------------------


@router.post(
    "/reports/{report_id}/items/{item_id}/verify",
    response_model=AltTextItemDTO,
    responses={404: {"model": ErrorDTO}, 409: {"model": ErrorDTO}},
)
async def verify_item(report_id: str, item_id: str, session: SessionDep) -> AltTextItemDTO:
    """Перевірити сторінку **без кешу** й зберегти вердикт.

    Запит іде з рандомним cache-buster-ом і `no-cache`-заголовками, бо в
    клієнтів кілька шарів кешу. Це зменшує, але не прибирає ризик edge-кешу —
    тому UI показує час перевірки й прямо про це попереджає.
    """

    def _read(sync_session: Session) -> tuple[list[str], str, str] | None:
        item = AltTextRepository(sync_session).get_item(report_id, item_id)
        if item is None:
            return None
        pages = list(item.page_urls_json or ([item.page_url] if item.page_url else []))
        return (pages, item.new_alt or "", item.image_url)

    found = await session.run_sync(_read)
    if found is None:
        raise HTTPException(status_code=404, detail=_ITEM_NOT_FOUND)
    pages, expected_alt, image_url = found
    if not pages:
        raise HTTPException(
            status_code=409,
            detail=_error(
                "no_page_url",
                "This row has no page URL, so there is nothing to check. "
                "Map a page column when importing the sheet.",
            ),
        )
    if not expected_alt.strip():
        raise HTTPException(
            status_code=409,
            detail=_error(
                "no_expected_alt",
                "This row has no new alt text to compare against.",
            ),
        )

    # Одна картинка стоїть на кількох сторінках, і alt у медіатеці WordPress
    # один — тож достатньо знайти його на будь-якій із них. Ідемо по черзі до
    # першого `match`, але не більше `MAX_VERIFY_PAGES` за клік: кожна ітерація
    # — це справжній запит до сайту клієнта.
    checked = pages[:MAX_VERIFY_PAGES]
    result = None
    for index, page_url in enumerate(checked):
        result = await run_in_threadpool(verify_alt, page_url, image_url, expected_alt)
        if result.status is VerifyStatus.match:
            break
        if index + 1 < len(checked):
            continue
    assert result is not None
    if len(checked) > 1:
        suffix = f"Checked {len(checked)} of {len(pages)} page(s) where this image is used."
        result = replace(result, detail=f"{result.detail} {suffix}" if result.detail else suffix)
    return await _store_verify(session, report_id, item_id, result)


async def _store_verify(
    session: AsyncSession, report_id: str, item_id: str, result: VerifyResult
) -> AltTextItemDTO:
    def _write(sync_session: Session) -> AltTextItemDTO | None:
        repository = AltTextRepository(sync_session)
        item = repository.get_item(report_id, item_id)
        if item is None:
            return None
        repository.set_verify_result(
            item,
            status=result.status.value,
            found_alt=result.found_alt,
            http_status=result.http_status,
            detail=(result.detail or None) and str(result.detail)[:500],
        )
        return _item_dto(item)

    dto = await session.run_sync(_write)
    if dto is None:
        raise HTTPException(status_code=404, detail=_ITEM_NOT_FOUND)
    await session.commit()
    return dto


# --- WordPress media ID (task-39) -----------------------------------------------


def _apply_canonical_www(host: str, canonical_www: bool | None) -> str:
    """Та сама поправка хоста, що й `app.monitor.runner._base_url` (task-38

    follow-up): `None` — оператор не задавав, хост лишається як записаний;
    `True`/`False` вибирає www/non-www напряму замість того, щоб кожен
    REST-запит з'їдав власний редірект сайту.
    """
    has_www = host.lower().startswith("www.")
    if canonical_www is True and not has_www:
        return f"www.{host}"
    if canonical_www is False and has_www:
        return host[len("www.") :]
    return host


def _report_site_host(sync_session: Session, report_id: str) -> str | None:
    """Домен клієнта звіту (з поправкою www/non-www із Site Monitor, якщо

    оператор її задав) — фолбек-корінь, коли хост картинки не WordPress.
    """
    report = AltTextRepository(sync_session).get_report(report_id)
    if report is None or report.client_id is None:
        return None
    client = ClientRepository(sync_session).get(report.client_id)
    if client is None:
        return None
    settings = MonitorRepository(sync_session).get_settings(report.client_id)
    canonical_www = settings.canonical_www if settings else None
    return _apply_canonical_www(client.domain, canonical_www)


def _lookup_dict(lookup: wp_rest.MediaLookup) -> dict[str, Any]:
    return {
        "media_id": lookup.media_id,
        "status": lookup.status.value,
        "confidence": lookup.confidence.value if lookup.confidence else None,
        "alt_text": lookup.alt_text,
        "admin_url": lookup.admin_url,
        "detail": lookup.detail,
        "candidates": [
            {
                "id": candidate.id,
                "file": candidate.file,
                "source_url": candidate.source_url,
                "alt_text": candidate.alt_text,
                "title": candidate.title,
            }
            for candidate in lookup.candidates
        ],
    }


def _resolve_media_sync(
    image_url: str, url_kind: str, search_key: str, *, site_host: str | None = None
) -> dict[str, Any]:
    """Знайти точний `attachment ID`. Блокуюча (до 6 запитів) — через `run_in_threadpool`.

    Не-uploads URL і закороткий фрагмент — `not_searchable` без жодного
    мережевого запиту: цей стан ставиться тут, а не всередині `wp_rest`,
    бо `wp_rest.lookup_media()` завжди отримує вже валідний `UploadLocation`.

    Хост картинки не завжди хост WordPress: бувають кастомні CDN-піддомени
    (`cdn.client.com`), яких `derive_upload_location`'s CDN-розпізнавання не
    знає (на відміну від Photon/Cloudflare/Statically — ті вже розгорнуті).
    Такий хост не має REST API → `rest_unavailable`. Якщо в нас є домен
    клієнта і він інший — пробуємо ще раз з ним як коренем, перш ніж здатися.
    """
    if not is_searchable(url_kind, search_key):
        return {
            "media_id": None,
            "status": "not_searchable",
            "confidence": None,
            "alt_text": None,
            "admin_url": None,
            "detail": "Not in the uploads folder — it cannot be in the media library.",
            "candidates": [],
        }
    location = derive_upload_location(image_url)
    if location is None:
        return {
            "media_id": None,
            "status": "not_searchable",
            "confidence": None,
            "alt_text": None,
            "admin_url": None,
            "detail": "Could not determine the WordPress uploads path from this URL.",
            "candidates": [],
        }
    lookup = wp_rest.lookup_media(location)
    if lookup.status is wp_rest.MediaLookupStatus.rest_unavailable and site_host:
        image_host = urlsplit(location.wp_root).hostname or ""
        client_host = site_host.strip().lower().removeprefix("www.")
        if client_host and image_host.removeprefix("www.") != client_host:
            alt_lookup = wp_rest.lookup_media(replace(location, wp_root=f"https://{site_host}"))
            if alt_lookup.status is not wp_rest.MediaLookupStatus.rest_unavailable:
                lookup = alt_lookup
    return _lookup_dict(lookup)


async def _store_media_result(
    session: AsyncSession, report_id: str, item_id: str, result: dict[str, Any]
) -> AltTextItemDTO:
    def _write(sync_session: Session) -> AltTextItemDTO | None:
        repository = AltTextRepository(sync_session)
        item = repository.get_item(report_id, item_id)
        if item is None:
            return None
        repository.set_media_result(
            item,
            media_id=result["media_id"],
            status=result["status"],
            confidence=result["confidence"],
            alt_text=result["alt_text"],
            admin_url=result["admin_url"],
            detail=result["detail"],
            candidates=result["candidates"],
        )
        return _item_dto(item)

    dto = await session.run_sync(_write)
    if dto is None:
        raise HTTPException(status_code=404, detail=_ITEM_NOT_FOUND)
    await session.commit()
    return dto


@router.post(
    "/reports/{report_id}/items/{item_id}/resolve-media",
    response_model=AltTextItemDTO,
    responses={404: {"model": ErrorDTO}},
)
async def resolve_item_media(report_id: str, item_id: str, session: SessionDep) -> AltTextItemDTO:
    """Знайти точний WordPress media ID для одного рядка й зберегти результат."""

    def _read(sync_session: Session) -> tuple[str, str, str, str | None] | None:
        repository = AltTextRepository(sync_session)
        item = repository.get_item(report_id, item_id)
        if item is None:
            return None
        site_host = _report_site_host(sync_session, report_id)
        return (item.image_url, item.url_kind, item.search_key, site_host)

    found = await session.run_sync(_read)
    if found is None:
        raise HTTPException(status_code=404, detail=_ITEM_NOT_FOUND)
    image_url, url_kind, search_key, site_host = found
    result = await run_in_threadpool(
        _resolve_media_sync, image_url, url_kind, search_key, site_host=site_host
    )
    return await _store_media_result(session, report_id, item_id, result)


@router.post(
    "/reports/{report_id}/resolve-media",
    response_model=AltTextMediaBatchDTO,
    responses={404: {"model": ErrorDTO}},
)
async def resolve_report_media(
    report_id: str, session: SessionDep, body: AltTextMediaBatchRequest | None = None
) -> AltTextMediaBatchDTO:
    """Пакетно знайти ID для рядків, які ще не шукали — до `limit` за раз.

    Послідовно, рядок за рядком: це реальні запити до сайту клієнта, а не
    наш ресурс, тож паралелити їх — навантажувальний тест чужого сервера.
    """
    limit = (body or AltTextMediaBatchRequest()).limit

    def _read(sync_session: Session) -> tuple[str | None, list[tuple[str, str, str, str]]] | None:
        repository = AltTextRepository(sync_session)
        if repository.get_report(report_id) is None:
            return None
        rows = repository.list_unresolved_items(report_id, limit=limit)
        site_host = _report_site_host(sync_session, report_id)
        return site_host, [(row.id, row.image_url, row.url_kind, row.search_key) for row in rows]

    found = await session.run_sync(_read)
    if found is None:
        raise HTTPException(status_code=404, detail=_REPORT_NOT_FOUND)
    site_host, items = found

    counts = {
        "resolved": 0,
        "ambiguous": 0,
        "not_found": 0,
        "rest_unavailable": 0,
        "failed": 0,
        "skipped": 0,
    }
    for item_id, image_url, url_kind, search_key in items:
        result = await run_in_threadpool(
            _resolve_media_sync, image_url, url_kind, search_key, site_host=site_host
        )
        if result["status"] == "not_searchable":
            counts["skipped"] += 1
        else:
            counts[result["status"]] = counts.get(result["status"], 0) + 1
        await _store_media_result(session, report_id, item_id, result)

    remaining = await session.run_sync(lambda s: AltTextRepository(s).unresolved_count(report_id))
    return AltTextMediaBatchDTO(**counts, remaining=remaining)


# --- видалення -----------------------------------------------------------------


@router.delete(
    "/reports/{report_id}",
    status_code=204,
    # `response_model=None` явно: під `from __future__ import annotations`
    # FastAPI інакше виводить модель із `-> None` і падає на «204 must not
    # have a response body».
    response_model=None,
    responses={404: {"model": ErrorDTO}},
)
async def delete_report(report_id: str, session: SessionDep) -> Response:
    deleted = await session.run_sync(lambda s: AltTextRepository(s).delete_report(report_id))
    if not deleted:
        raise HTTPException(status_code=404, detail=_REPORT_NOT_FOUND)
    await session.commit()
    return Response(status_code=204)


# --- експорт -------------------------------------------------------------------

_EXPORT_HEADER = (
    "#",
    "Image URL",
    "Search key",
    "Fallback search key",
    "URL kind",
    "Stripped",
    "New alt",
    "Current alt",
    "Page URL",
    "Status",
    "Verify",
    "Alt found on page",
    "Checked at",
    "Media ID",
    "Media status",
    "Admin URL",
)
# Клітинка, що починається з цих символів, у Excel/Sheets стає формулою —
# експорт звіту не має виконувати нічого з чужого файлу.
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _safe_cell(value: object) -> str:
    text = "" if value is None else str(value)
    return f"'{text}" if text.startswith(_FORMULA_PREFIXES) else text


def _export_values(item: AltTextItem) -> tuple[str, ...]:
    derivation = item.derivation_json or {}
    return tuple(
        _safe_cell(value)
        for value in (
            item.row_index,
            item.image_url,
            item.search_key,
            item.search_key_fallback or "",
            item.url_kind,
            " ".join(derivation.get("stripped") or []),
            item.new_alt,
            item.old_alt or "",
            item.page_url or "",
            item.status,
            item.verify_status or "",
            item.verify_alt or "",
            item.verify_checked_at.isoformat() if item.verify_checked_at else "",
            item.wp_media_id if item.wp_media_id is not None else "",
            item.wp_media_status or "",
            item.wp_media_admin_url or "",
        )
    )


def _content_disposition(filename: str) -> str:
    """`Content-Disposition` для назви з не-ASCII символами.

    HTTP-заголовки кодуються latin-1, тож будь-яке тире «—» чи кирилиця в
    назві звіту валили відповідь `UnicodeEncodeError` (500). Даємо ASCII-версію
    для сумісності плюс `filename*` (RFC 5987) з повною назвою.
    """
    ascii_name = (
        unicodedata.normalize("NFKD", filename)
        .encode("ascii", "ignore")
        .decode("ascii")
        .replace('"', "")
        .strip()
        or "report"
    )
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename)}"


def _export_rows(session: Session, report_id: str) -> tuple[str, list[tuple[str, ...]]] | None:
    repository = AltTextRepository(session)
    report = repository.get_report(report_id)
    if report is None:
        return None
    slug = report.title.strip().upper().replace(" ", "-").replace("/", "-")[:60] or "REPORT"
    return slug, [_export_values(item) for item in repository.all_items(report_id)]


@router.get("/reports/{report_id}/export.csv", responses={404: {"model": ErrorDTO}})
async def export_csv(report_id: str, session: SessionDep) -> StreamingResponse:
    exported = await session.run_sync(lambda s: _export_rows(s, report_id))
    if exported is None:
        raise HTTPException(status_code=404, detail=_REPORT_NOT_FOUND)
    slug, rows = exported

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(_EXPORT_HEADER)
    writer.writerows(rows)
    filename = f"{slug}_AltText_{date.today().strftime('%Y%m%d')}.csv"
    return StreamingResponse(
        iter([buffer.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": _content_disposition(filename)},
    )


@router.get("/reports/{report_id}/export.xlsx", responses={404: {"model": ErrorDTO}})
async def export_xlsx(report_id: str, session: SessionDep) -> StreamingResponse:
    exported = await session.run_sync(lambda s: _export_rows(s, report_id))
    if exported is None:
        raise HTTPException(status_code=404, detail=_REPORT_NOT_FOUND)
    slug, rows = exported

    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "Image alt text"
    worksheet.append(_EXPORT_HEADER)
    for row in rows:
        worksheet.append(row)
    for column_cells in worksheet.columns:
        length = max(
            (len(str(cell.value)) for cell in column_cells if cell.value is not None), default=10
        )
        worksheet.column_dimensions[column_cells[0].column_letter].width = min(length + 2, 80)

    buffer = io.BytesIO()
    workbook.save(buffer)
    filename = f"{slug}_AltText_{date.today().strftime('%Y%m%d')}.xlsx"
    return StreamingResponse(
        iter([buffer.getvalue()]),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": _content_disposition(filename)},
    )


register_router(router)


# Прапорець вердикту, який фронт показує окремо від статусу прогресу.
VERIFY_STATUSES = tuple(status.value for status in VerifyStatus)
