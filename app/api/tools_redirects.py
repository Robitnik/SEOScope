"""Redirect Checker endpoints (task-29).

Свідомо **не** через job-рушій (той самий вибір, що в `tools_alt_text.py`): один
трейс — це кілька HTTP-запитів на секунди, а не колекція метрик за період.
Список URL проганяє фронт, викликаючи `…/targets/{id}/run` по рядку: прогрес
живе в БД (переживає F5), а `SyncJob`/SSE/lease не потрібні.

Politeness-гард (один запит на хост + мінімальний інтервал) сидить у самому
трейсері, тобто на сервері: покладатися в такому на клієнтський JS не можна.

Кожен мутуючий ендпоінт комітить сам — `app.db.session.get_db` віддає сесію без
auto-commit (той самий коментар — у `api/clients.py`).
"""

from __future__ import annotations

import csv
import io
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Literal
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Query, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse
from openpyxl import Workbook
from sqlalchemy.orm import Session

from app.api import register_router
from app.api.deps import PaginationDep, SessionDep
from app.db.base import utcnow
from app.db.models import RedirectCheck, RedirectCheckTarget
from app.db.repositories.clients import ClientRepository
from app.db.repositories.redirects import (
    RedirectRepository,
    TargetFilters,
    TargetInput,
    TargetResultInput,
)
from app.schemas.api import ErrorDTO
from app.schemas.tools import (
    RedirectCheckCreateRequest,
    RedirectCheckDTO,
    RedirectCheckSummaryDTO,
    RedirectFinalPageDTO,
    RedirectHopDTO,
    RedirectOptionsDTO,
    RedirectTargetDTO,
    RedirectTargetsPageDTO,
    RedirectTraceDTO,
    RedirectTraceRequest,
    RejectedUrlDTO,
)
from app.tools.redirects.input import MAX_URLS, parse_url_list
from app.tools.redirects.trace import TraceOptions, TraceResult, trace_redirects

router = APIRouter(prefix="/api/v1/tools/redirects", tags=["tools"])

_CHECK_NOT_FOUND = ErrorDTO(error_code="check_not_found", message="Check not found").model_dump()
_TARGET_NOT_FOUND = ErrorDTO(error_code="target_not_found", message="Row not found").model_dump()


def _error(code: str, message: str, **details: Any) -> dict[str, Any]:
    return ErrorDTO(error_code=code, message=message, details=details).model_dump()


def _options(raw: RedirectOptionsDTO | None) -> TraceOptions:
    return TraceOptions.clamped(raw.model_dump() if raw else None)


# --- DTO ----------------------------------------------------------------------


def _hop_dtos(chain: list[dict[str, Any]]) -> list[RedirectHopDTO]:
    return [RedirectHopDTO.model_validate(hop) for hop in chain]


def _trace_dto(result: TraceResult) -> RedirectTraceDTO:
    return RedirectTraceDTO(
        input_url=result.input_url,
        normalized_url=result.normalized_url,
        verdict=result.verdict.value,
        hops=_hop_dtos([hop.as_dict() for hop in result.hops]),
        hop_count=result.hop_count,
        final_url=result.final_url,
        final_status=result.final_status,
        flags=list(result.flags),
        final_page=(
            RedirectFinalPageDTO.model_validate(result.final_page.as_dict())
            if result.final_page
            else None
        ),
        error_code=result.error_code,
        error_detail=result.error_detail,
        elapsed_ms=result.elapsed_ms,
        checked_at=utcnow(),
    )


def _target_dto(target: RedirectCheckTarget) -> RedirectTargetDTO:
    final_page = target.final_page_json or None
    return RedirectTargetDTO(
        id=target.id,
        row_index=target.row_index,
        run_status=target.run_status,  # type: ignore[arg-type]
        input_url=target.input_url,
        normalized_url=target.normalized_url,
        input_notes=list(target.input_notes_json or []),
        verdict=target.verdict,  # type: ignore[arg-type]
        hops=_hop_dtos(list(target.chain_json or [])),
        # `None`, а не `0`: «не перевіряли» ≠ «редіректів немає» (PLAN §12).
        hop_count=target.hop_count,
        final_url=target.final_url,
        final_status=target.final_status_code,
        flags=list(target.flags_json or []),
        final_page=RedirectFinalPageDTO.model_validate(final_page) if final_page else None,
        error_code=target.error_code,
        error_detail=target.error_detail,
        elapsed_ms=target.elapsed_ms,
        checked_at=target.checked_at,
    )


def _summary_dto(
    check: RedirectCheck,
    *,
    run_status_counts: dict[str, int],
    verdict_counts: dict[str, int],
    client_name: str | None,
) -> RedirectCheckSummaryDTO:
    return RedirectCheckSummaryDTO(
        id=check.id,
        title=check.title,
        client_id=check.client_id,
        client_name=client_name,
        input_count=check.input_count,
        target_count=check.target_count,
        run_status_counts=run_status_counts,
        verdict_counts=verdict_counts,
        created_at=check.created_at,
        updated_at=check.updated_at,
    )


def _check_dto(session: Session, check: RedirectCheck) -> RedirectCheckDTO:
    repository = RedirectRepository(session)
    client_name = None
    if check.client_id:
        client = ClientRepository(session).get(check.client_id)
        client_name = client.name if client else None
    summary = _summary_dto(
        check,
        run_status_counts=repository.run_status_counts(check.id),
        verdict_counts=repository.verdict_counts(check.id),
        client_name=client_name,
    )
    return RedirectCheckDTO(
        **summary.model_dump(),
        options=RedirectOptionsDTO.model_validate(check.options_json or {}),
        rejected=[RejectedUrlDTO.model_validate(row) for row in (check.rejected_json or [])],
        duplicate_count=check.duplicate_count,
        truncated=check.truncated,
    )


# --- швидкий трейс без збереження ----------------------------------------------


@router.post("/trace", response_model=RedirectTraceDTO)
async def trace_one(body: RedirectTraceRequest) -> RedirectTraceDTO:
    """Вставили посилання — побачили ланцюжок. Нічого не створюємо.

    Головний шлях інструмента: нуль кліків, нуль сутностей. Хто хоче історію,
    натискає «Save as check» і йде через `POST /checks`.
    """
    result = await run_in_threadpool(trace_redirects, body.url, options=_options(body.options))
    return _trace_dto(result)


# --- прогони -------------------------------------------------------------------


@router.post(
    "/checks",
    response_model=RedirectCheckDTO,
    status_code=201,
    responses={404: {"model": ErrorDTO}, 413: {"model": ErrorDTO}, 422: {"model": ErrorDTO}},
)
async def create_check(body: RedirectCheckCreateRequest, session: SessionDep) -> RedirectCheckDTO:
    """Створити прогін: нормалізувати список і поставити рядки в `queued`.

    Сам обхід тут **не** відбувається: 200 URL — це хвилини реальних запитів,
    тобто не один HTTP-запит. Рядки проганяє фронт по одному.
    """
    parsed = await run_in_threadpool(parse_url_list, "\n".join(body.urls))
    if not parsed.entries:
        raise HTTPException(
            status_code=422,
            detail=_error(
                "no_valid_urls",
                "None of the pasted lines is an http:// or https:// link.",
                rejected=[row.error_code for row in parsed.rejected],
            ),
        )
    if parsed.found_count > MAX_URLS * 5:
        raise HTTPException(
            status_code=413,
            detail=_error(
                "too_many_urls",
                f"That is more than {MAX_URLS * 5} lines. Split the list into smaller checks.",
            ),
        )

    client_name = None
    if body.client_id is not None:
        client = await session.run_sync(lambda s: ClientRepository(s).get(body.client_id or ""))
        if client is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorDTO(
                    error_code="client_not_found", message="Client not found"
                ).model_dump(),
            )
        client_name = client.name

    options = _options(body.options)
    title = (body.title or "").strip() or _default_title(len(parsed.entries), client_name)

    def _persist(sync_session: Session) -> RedirectCheckDTO:
        check = RedirectRepository(sync_session).create_check(
            client_id=body.client_id,
            title=title[:200],
            input_count=parsed.found_count,
            duplicate_count=parsed.duplicate_count,
            truncated=parsed.truncated,
            options=options.as_dict(),
            rejected=[
                {
                    "input_url": row.input_url,
                    "error_code": row.error_code,
                    "message": row.message,
                }
                for row in parsed.rejected
            ],
            targets=[
                TargetInput(
                    row_index=index,
                    input_url=entry.input_url,
                    normalized_url=entry.url,
                    notes=entry.notes,
                )
                for index, entry in enumerate(parsed.entries, start=1)
            ],
        )
        return _check_dto(sync_session, check)

    dto = await session.run_sync(_persist)
    await session.commit()
    return dto


def _default_title(count: int, client_name: str | None) -> str:
    label = f"{count} link" if count == 1 else f"{count} links"
    prefix = f"{client_name} · " if client_name else ""
    return f"{prefix}Redirects · {date.today().strftime('%d %b')} · {label}"


@router.get("/checks", response_model=list[RedirectCheckSummaryDTO])
async def list_checks(
    session: SessionDep, pagination: PaginationDep
) -> list[RedirectCheckSummaryDTO]:
    def _read(sync_session: Session) -> list[RedirectCheckSummaryDTO]:
        repository = RedirectRepository(sync_session)
        checks, _total = repository.list_checks(
            limit=pagination.limit, offset=pagination.offset
        )
        clients = ClientRepository(sync_session)
        summaries = []
        for check in checks:
            client_name = None
            if check.client_id:
                client = clients.get(check.client_id)
                client_name = client.name if client else None
            summaries.append(
                _summary_dto(
                    check,
                    run_status_counts=repository.run_status_counts(check.id),
                    verdict_counts=repository.verdict_counts(check.id),
                    client_name=client_name,
                )
            )
        return summaries

    return await session.run_sync(_read)


@router.get(
    "/checks/{check_id}", response_model=RedirectCheckDTO, responses={404: {"model": ErrorDTO}}
)
async def get_check(check_id: str, session: SessionDep) -> RedirectCheckDTO:
    def _read(sync_session: Session) -> RedirectCheckDTO | None:
        check = RedirectRepository(sync_session).get_check(check_id)
        return None if check is None else _check_dto(sync_session, check)

    dto = await session.run_sync(_read)
    if dto is None:
        raise HTTPException(status_code=404, detail=_CHECK_NOT_FOUND)
    return dto


@router.get(
    "/checks/{check_id}/targets",
    response_model=RedirectTargetsPageDTO,
    responses={404: {"model": ErrorDTO}},
)
async def list_targets(
    check_id: str,
    session: SessionDep,
    q: str | None = None,
    verdict: str | None = None,
    run_status: str | None = None,
    flag: str | None = None,
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> RedirectTargetsPageDTO:
    filters = TargetFilters(query=q, verdict=verdict, run_status=run_status, flag=flag)

    def _read(sync_session: Session) -> RedirectTargetsPageDTO | None:
        repository = RedirectRepository(sync_session)
        if repository.get_check(check_id) is None:
            return None
        rows, total = repository.list_targets(
            check_id, filters=filters, limit=limit, offset=offset
        )
        return RedirectTargetsPageDTO(
            check_id=check_id,
            total=total,
            limit=limit,
            offset=offset,
            rows=[_target_dto(row) for row in rows],
        )

    page = await session.run_sync(_read)
    if page is None:
        raise HTTPException(status_code=404, detail=_CHECK_NOT_FOUND)
    return page


# --- прогін одного рядка -------------------------------------------------------


@dataclass(frozen=True)
class _Claim:
    """Результат спроби взяти рядок у роботу (404 / уже виконується / взяли)."""

    outcome: Literal["not_found", "busy", "claimed"]
    url: str = ""
    options: dict[str, Any] = field(default_factory=dict)


@router.post(
    "/checks/{check_id}/targets/{target_id}/run",
    response_model=RedirectTargetDTO,
    responses={404: {"model": ErrorDTO}, 409: {"model": ErrorDTO}},
)
async def run_target(check_id: str, target_id: str, session: SessionDep) -> RedirectTargetDTO:
    """Пройти ланцюжок одного рядка й зберегти результат.

    Повторний виклик легальний (перезапуск рядка), але **паралельний** — ні:
    `claim_target` атомарно бере рядок у роботу, тож подвійний клік не дає двох
    запитів до чужого сайту.
    """

    def _claim(sync_session: Session) -> _Claim:
        repository = RedirectRepository(sync_session)
        check = repository.get_check(check_id)
        if check is None or repository.get_target(check_id, target_id) is None:
            return _Claim(outcome="not_found")
        claimed = repository.claim_target(check_id, target_id)
        if claimed is None:
            return _Claim(outcome="busy")
        return _Claim(
            outcome="claimed", url=claimed.normalized_url, options=dict(check.options_json or {})
        )

    claim = await session.run_sync(_claim)
    if claim.outcome == "not_found":
        raise HTTPException(status_code=404, detail=_TARGET_NOT_FOUND)
    if claim.outcome == "busy":
        raise HTTPException(
            status_code=409,
            detail=_error("target_running", "This row is already being checked."),
        )
    await session.commit()
    url, raw_options = claim.url, claim.options

    try:
        result = await run_in_threadpool(
            trace_redirects, url, options=TraceOptions.clamped(raw_options)
        )
    except Exception as exc:  # noqa: BLE001 - рядок не має лишитись у `running`
        # Назву типу забираємо одразу: `except ... as exc` зникає з області
        # видимості наприкінці блоку, а замикання читає її пізніше.
        reason = type(exc).__name__

        def _release(sync_session: Session) -> None:
            repository = RedirectRepository(sync_session)
            target = repository.get_target(check_id, target_id)
            if target is not None:
                repository.release_target(target, error_detail=reason)

        await session.run_sync(_release)
        await session.commit()
        raise HTTPException(
            status_code=500,
            detail=_error("run_failed", "The check could not be completed."),
        ) from exc

    def _save(sync_session: Session) -> RedirectTargetDTO | None:
        repository = RedirectRepository(sync_session)
        target = repository.get_target(check_id, target_id)
        if target is None:
            return None
        repository.save_result(
            target,
            TargetResultInput(
                verdict=result.verdict.value,
                final_url=result.final_url,
                final_status_code=result.final_status,
                hop_count=result.hop_count,
                chain=[hop.as_dict() for hop in result.hops],
                flags=list(result.flags),
                final_page=result.final_page.as_dict() if result.final_page else None,
                error_code=result.error_code,
                error_detail=result.error_detail,
                elapsed_ms=result.elapsed_ms,
            ),
        )
        return _target_dto(target)

    dto = await session.run_sync(_save)
    if dto is None:
        raise HTTPException(status_code=404, detail=_TARGET_NOT_FOUND)
    await session.commit()
    return dto


# --- видалення -----------------------------------------------------------------


@router.delete(
    "/checks/{check_id}",
    status_code=204,
    # `response_model=None` явно: під `from __future__ import annotations`
    # FastAPI інакше виводить модель із `-> None` і падає на 204.
    response_model=None,
    responses={404: {"model": ErrorDTO}},
)
async def delete_check(check_id: str, session: SessionDep) -> Response:
    deleted = await session.run_sync(lambda s: RedirectRepository(s).delete_check(check_id))
    if not deleted:
        raise HTTPException(status_code=404, detail=_CHECK_NOT_FOUND)
    await session.commit()
    return Response(status_code=204)


# --- експорт -------------------------------------------------------------------

_EXPORT_HEADER = (
    "#",
    "Input URL",
    "Checked URL",
    "Verdict",
    "Redirects",
    "Final URL",
    "Final status",
    "Chain",
    "Flags",
    "Final title",
    "Canonical",
    "Error",
    "Checked at",
)
# Клітинка, що починається з цих символів, у Excel/Sheets стає формулою —
# експорт не має виконувати нічого з чужого сайту.
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _safe_cell(value: object) -> str:
    text = "" if value is None else str(value)
    return f"'{text}" if text.startswith(_FORMULA_PREFIXES) else text


def chain_as_text(chain: list[dict[str, Any]]) -> str:
    """`301 → 301 → 200` — ланцюжок, який читається одним поглядом."""
    return " → ".join(str(hop.get("status", "?")) for hop in chain)


def _export_values(target: RedirectCheckTarget) -> tuple[str, ...]:
    page = target.final_page_json or {}
    return tuple(
        _safe_cell(value)
        for value in (
            target.row_index,
            target.input_url,
            target.normalized_url,
            target.verdict or "",
            # Порожньо, а не `0`: неперевірений рядок не має «нуля редіректів».
            "" if target.hop_count is None else target.hop_count,
            target.final_url or "",
            target.final_status_code or "",
            chain_as_text(list(target.chain_json or [])),
            " ".join(target.flags_json or []),
            page.get("title") or "",
            page.get("canonical") or "",
            target.error_detail or "",
            target.checked_at.isoformat() if target.checked_at else "",
        )
    )


def _content_disposition(filename: str) -> str:
    """`Content-Disposition` для назви з не-ASCII символами.

    HTTP-заголовки кодуються latin-1, тож тире «—» чи кирилиця в назві прогону
    інакше валили б відповідь `UnicodeEncodeError` (той самий дефект, який
    task-27 знайшов і виправив у себе).
    """
    ascii_name = (
        unicodedata.normalize("NFKD", filename)
        .encode("ascii", "ignore")
        .decode("ascii")
        .replace('"', "")
        .strip()
        or "redirects"
    )
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename)}"


def _export_rows(
    session: Session, check_id: str
) -> tuple[str, list[tuple[str, ...]], list[tuple[str, ...]]] | None:
    repository = RedirectRepository(session)
    check = repository.get_check(check_id)
    if check is None:
        return None
    slug = check.title.strip().upper().replace(" ", "-").replace("/", "-")[:60] or "REDIRECTS"
    targets = repository.all_targets(check_id)
    rows = [_export_values(target) for target in targets]
    # Другий аркуш: по рядку на хоп — для тих, хто хоче розібрати ланцюжок.
    hops: list[tuple[str, ...]] = []
    for target in targets:
        for hop in target.chain_json or []:
            hops.append(
                tuple(
                    _safe_cell(value)
                    for value in (
                        target.row_index,
                        hop.get("index"),
                        hop.get("url"),
                        hop.get("status"),
                        hop.get("status_text"),
                        hop.get("kind"),
                        hop.get("permanence") or "",
                        hop.get("location") or "",
                        " ".join(hop.get("changes") or []),
                        hop.get("elapsed_ms") or "",
                    )
                )
            )
    return slug, rows, hops


_HOP_HEADER = (
    "#",
    "Hop",
    "URL",
    "Status",
    "Status text",
    "Kind",
    "Permanence",
    "Location",
    "Changes",
    "Elapsed ms",
)


@router.get("/checks/{check_id}/export.csv", responses={404: {"model": ErrorDTO}})
async def export_csv(check_id: str, session: SessionDep) -> StreamingResponse:
    exported = await session.run_sync(lambda s: _export_rows(s, check_id))
    if exported is None:
        raise HTTPException(status_code=404, detail=_CHECK_NOT_FOUND)
    slug, rows, _hops = exported

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(_EXPORT_HEADER)
    writer.writerows(rows)
    filename = f"{slug}_Redirects_{date.today().strftime('%Y%m%d')}.csv"
    return StreamingResponse(
        iter([buffer.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": _content_disposition(filename)},
    )


@router.get("/checks/{check_id}/export.xlsx", responses={404: {"model": ErrorDTO}})
async def export_xlsx(check_id: str, session: SessionDep) -> StreamingResponse:
    exported = await session.run_sync(lambda s: _export_rows(s, check_id))
    if exported is None:
        raise HTTPException(status_code=404, detail=_CHECK_NOT_FOUND)
    slug, rows, hops = exported

    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "Redirects"
    worksheet.append(_EXPORT_HEADER)
    for row in rows:
        worksheet.append(row)
    hop_sheet = workbook.create_sheet("Hops")
    hop_sheet.append(_HOP_HEADER)
    for hop in hops:
        hop_sheet.append(hop)
    for sheet in (worksheet, hop_sheet):
        for column_cells in sheet.columns:
            length = max(
                (len(str(cell.value)) for cell in column_cells if cell.value is not None),
                default=10,
            )
            sheet.column_dimensions[column_cells[0].column_letter].width = min(length + 2, 80)

    buffer = io.BytesIO()
    workbook.save(buffer)
    filename = f"{slug}_Redirects_{date.today().strftime('%Y%m%d')}.xlsx"
    return StreamingResponse(
        iter([buffer.getvalue()]),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": _content_disposition(filename)},
    )


register_router(router)
