"""Schema Markup Checker endpoints (task-56).

Свідомо **не** через job-рушій — той самий вибір, що в Alt Text/Redirect
Checker/Content QA: одна перевірка — один HTTP-запит до сторінки клієнта,
секунди, не колекція метрик за період. Мережевий фетч (`urllib`, блокуючий)
іде через `run_in_threadpool`, як і в `tools_alt_text.py`/`tools_redirects.py`.

Кожен мутуючий ендпоінт комітить сам — `app.db.session.get_db` (тут:
`SessionDep`) віддає сесію без auto-commit (той самий коментар, що в
`api/clients.py`/`tools_content_qa.py`).
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.concurrency import run_in_threadpool
from sqlalchemy.orm import Session

from app.api import register_router
from app.api.deps import PaginationDep, SessionDep
from app.db.models import SchemaMarkupCheck
from app.db.repositories.clients import ClientRepository
from app.db.repositories.schema_markup import SchemaMarkupCheckInput, SchemaMarkupRepository
from app.schemas.api import ErrorDTO
from app.schemas.schema_markup import (
    SchemaMarkupCheckDTO,
    SchemaMarkupCheckRequest,
    SchemaMarkupChecksPageDTO,
    SchemaMarkupCheckSummaryDTO,
    SchemaMarkupFindingDTO,
    SchemaMarkupGeneratedDTO,
    SchemaMarkupGeneratedEntityDTO,
    SchemaMarkupRawBlockDTO,
)
from app.tools.schema_markup.service import CheckResult, run_check

router = APIRouter(prefix="/api/v1/tools/schema-markup", tags=["schema-markup"])


def _error(code: str, message: str) -> dict[str, object]:
    return ErrorDTO(error_code=code, message=message).model_dump()


def _client_name(sync_session: Session, client_id: str | None) -> str | None:
    if not client_id:
        return None
    client = ClientRepository(sync_session).get(client_id)
    return client.name if client else None


def _generated_dto(generated_json: dict[str, object] | None) -> SchemaMarkupGeneratedDTO | None:
    if generated_json is None:
        return None
    return SchemaMarkupGeneratedDTO(**generated_json)  # type: ignore[arg-type]


def _summary_dto(
    check: SchemaMarkupCheck, *, client_name: str | None
) -> SchemaMarkupCheckSummaryDTO:
    return SchemaMarkupCheckSummaryDTO(
        id=check.id,
        client_id=check.client_id,
        client_name=client_name,
        url=check.url,
        final_url=check.final_url,
        verdict=check.verdict,  # type: ignore[arg-type]
        page_kind=check.page_kind,
        found_types=check.found_types_json,
        created_at=check.created_at,
    )


def _detail_dto(check: SchemaMarkupCheck, *, client_name: str | None) -> SchemaMarkupCheckDTO:
    summary = _summary_dto(check, client_name=client_name)
    return SchemaMarkupCheckDTO(
        **summary.model_dump(),
        http_status=check.http_status,
        findings=[SchemaMarkupFindingDTO(**f) for f in check.findings_json],
        generated=_generated_dto(check.generated_json),
        raw_markup=[SchemaMarkupRawBlockDTO(**b) for b in check.raw_markup_json],
        vocab_version=check.vocab_version,
        error=check.error,
    )


def _result_to_generated_json(result: CheckResult) -> dict[str, object] | None:
    if result.generated is None:
        return None
    return SchemaMarkupGeneratedDTO(
        entities=[
            SchemaMarkupGeneratedEntityDTO(
                type_name=e.type_name,
                jsonld=e.to_jsonld(),
                sources=e.sources,
                needs_input=e.needs_input,
            )
            for e in result.generated.entities
        ],
        skipped=[list(s) for s in result.generated.skipped],
    ).model_dump()


def _result_to_raw_markup_json(result: CheckResult) -> list[dict[str, object]]:
    """Сира розмітка як вона реально знайдена — окремо від `findings`/
    `found_types` (task-56 follow-up, запит оператора: бачити не лише список
    типів, а сам JSON). JSON-LD: оригінальний вузол/граф (`block.parsed`), або
    `None` + `error`, якщо блок був, але не розпарсився (L0). Microdata: своєї
    нативної JSON-форми немає, тож дерево серіалізується (`to_jsonable()`).
    """
    if result.extraction is None:
        return []
    blocks: list[dict[str, object]] = [
        {"format": "json-ld", "raw": block.parsed, "error": block.error}
        for block in result.extraction.json_ld_blocks
    ]
    blocks.extend(
        {"format": "microdata", "raw": entity.to_jsonable(), "error": None}
        for entity in result.extraction.microdata_entities
    )
    return blocks


def _result_to_findings_json(result: CheckResult) -> list[dict[str, object]]:
    return [
        {
            "level": f.level.value,
            "severity": f.severity.value,
            "code": f.code,
            "message": f.message,
            "entity_types": list(f.entity_types),
            "property_name": f.property_name,
        }
        for f in result.findings
    ]


@router.post("/checks", response_model=SchemaMarkupCheckDTO)
async def create_check(
    payload: SchemaMarkupCheckRequest, session: SessionDep
) -> SchemaMarkupCheckDTO:
    def _load_client_name(sync_session: Session) -> str | None:
        return _client_name(sync_session, payload.client_id)

    client_name = await session.run_sync(_load_client_name)
    result = await run_in_threadpool(
        run_check, payload.url, organization_name=client_name
    )

    def _persist(sync_session: Session) -> SchemaMarkupCheckDTO:
        check = SchemaMarkupRepository(sync_session).create_check(
            SchemaMarkupCheckInput(
                client_id=payload.client_id,
                url=result.url,
                final_url=result.final_url,
                http_status=result.http_status,
                verdict=result.verdict,
                page_kind=result.page_kind.kind if result.page_kind else None,
                found_types=result.found_types,
                findings=_result_to_findings_json(result),
                generated=_result_to_generated_json(result),
                raw_markup=_result_to_raw_markup_json(result),
                vocab_version=result.vocab_version,
                error=result.error,
            )
        )
        return _detail_dto(check, client_name=client_name)

    dto = await session.run_sync(_persist)
    await session.commit()
    return dto


@router.get("/checks", response_model=SchemaMarkupChecksPageDTO)
async def list_checks(
    session: SessionDep, pagination: PaginationDep, client_id: str | None = None
) -> SchemaMarkupChecksPageDTO:
    def _read(sync_session: Session) -> SchemaMarkupChecksPageDTO:
        checks, total = SchemaMarkupRepository(sync_session).list_checks(
            client_id=client_id, limit=pagination.limit, offset=pagination.offset
        )
        return SchemaMarkupChecksPageDTO(
            total=total,
            limit=pagination.limit,
            offset=pagination.offset,
            checks=[
                _summary_dto(c, client_name=_client_name(sync_session, c.client_id))
                for c in checks
            ],
        )

    return await session.run_sync(_read)


@router.get(
    "/checks/{check_id}",
    response_model=SchemaMarkupCheckDTO,
    responses={404: {"model": ErrorDTO}},
)
async def get_check(check_id: str, session: SessionDep) -> SchemaMarkupCheckDTO:
    def _read(sync_session: Session) -> SchemaMarkupCheckDTO | None:
        check = SchemaMarkupRepository(sync_session).get_check(check_id)
        if check is None:
            return None
        return _detail_dto(check, client_name=_client_name(sync_session, check.client_id))

    dto = await session.run_sync(_read)
    if dto is None:
        raise HTTPException(
            status_code=404,
            detail=_error("check_not_found", f"No schema markup check with id {check_id!r}."),
        )
    return dto


register_router(router)
