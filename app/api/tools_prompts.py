"""Prompt Launcher endpoints (task-94).

Read-only тул: усі три ролі (`admin`/`member`/`viewer`) бачать однакове —
`required_role` для safe-методів поза allowlist'ами повертає `viewer`
(`app/core/auth/policy.py`), тож жодних додаткових гейтів тут не треба.

Файлова частина (наявність/дата/розмір промта, читання `/source`) іде через
`run_in_threadpool`, як і в `tools_alt_text.py`; клієнтська частина (`/clients`)
іде через `session.run_sync`, як у `tools_schema_markup.py::get_check`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.concurrency import run_in_threadpool
from sqlalchemy.orm import Session

from app.api import register_router
from app.api.deps import SessionDep, SettingsDep
from app.core.config import Settings
from app.core.security import PathTraversalError, safe_resolve
from app.db.models import Client
from app.db.repositories.clients import ClientRepository
from app.prompts.registry import TEMPLATES, PromptTemplate, get_template
from app.prompts.render import render_for_client
from app.schemas.api import ErrorDTO
from app.schemas.enums import ClientStatus
from app.schemas.prompts import (
    PromptClientRowDTO,
    PromptClientsDTO,
    PromptFieldDTO,
    PromptSourceDTO,
    PromptTemplateDTO,
    PromptTemplatesDTO,
)

router = APIRouter(prefix="/api/v1/tools/prompts", tags=["prompts"])

# Лімітуємо перегляд джерела — панель "Prompt source" в UI, не завантаження
# файлу; жоден наявний промт не наближається до цього розміру.
_SOURCE_MAX_BYTES = 512_000


def _error(code: str, message: str) -> dict[str, object]:
    return ErrorDTO(error_code=code, message=message).model_dump()


def _host_doc_path(settings: Settings, template: PromptTemplate) -> str:
    return f"{settings.aiseotools_host_path.rstrip('/')}/{template.doc_relpath}"


@dataclass(frozen=True)
class _DocStat:
    available: bool
    updated_at: datetime | None
    size_bytes: int | None


def _stat_doc(settings: Settings, template: PromptTemplate) -> _DocStat:
    try:
        path = safe_resolve(settings.aiseotools_source_path, template.doc_relpath)
    except PathTraversalError:
        return _DocStat(available=False, updated_at=None, size_bytes=None)
    if not path.is_file():
        return _DocStat(available=False, updated_at=None, size_bytes=None)
    info = path.stat()
    return _DocStat(
        available=True,
        updated_at=datetime.fromtimestamp(info.st_mtime, tz=UTC),
        size_bytes=info.st_size,
    )


def _template_dto(settings: Settings, template: PromptTemplate) -> PromptTemplateDTO:
    doc_stat = _stat_doc(settings, template)
    return PromptTemplateDTO(
        key=template.key,
        name=template.name,
        summary=template.summary,
        doc_relpath=template.doc_relpath,
        doc_host_path=_host_doc_path(settings, template),
        doc_available=doc_stat.available,
        doc_updated_at=doc_stat.updated_at,
        doc_bytes=doc_stat.size_bytes,
        fields=[PromptFieldDTO(key=f.key, label=f.label) for f in template.fields],
    )


@router.get("", response_model=PromptTemplatesDTO)
async def list_templates(settings: SettingsDep) -> PromptTemplatesDTO:
    def _read() -> PromptTemplatesDTO:
        return PromptTemplatesDTO(templates=[_template_dto(settings, t) for t in TEMPLATES])

    return await run_in_threadpool(_read)


def _client_row(
    client: Client, template: PromptTemplate, session: Session, *, doc_host_path: str
) -> PromptClientRowDTO:
    values, rendered, blocked_reason = render_for_client(
        template, client, session, doc_host_path=doc_host_path
    )
    return PromptClientRowDTO(
        client_id=client.id,
        client_name=client.name,
        slug=client.slug,
        status=client.status.value,
        enabled=client.enabled,
        is_dev=client.is_dev,
        collectable=client.enabled and client.status == ClientStatus.active,
        values=values,
        rendered=rendered,
        blocked_reason=blocked_reason,
    )


@router.get(
    "/{key}/clients",
    response_model=PromptClientsDTO,
    responses={404: {"model": ErrorDTO}},
)
async def list_clients(key: str, session: SessionDep, settings: SettingsDep) -> PromptClientsDTO:
    template = get_template(key)
    if template is None:
        raise HTTPException(
            status_code=404,
            detail=_error("template_not_found", f"No prompt template named {key!r}."),
        )
    doc_host_path = _host_doc_path(settings, template)

    def _read(sync_session: Session) -> PromptClientsDTO:
        repo = ClientRepository(sync_session)
        # Усі клієнти (`list_all`), не лише collectable: `collectable` на
        # кожному рядку каже фронту, кого показувати одразу (той самий набір,
        # у який розгортається масовий збір), а кого — лише за перемикачем
        # "Show paused clients" (§2 таск-плану). Фронт групує за готовим
        # прапорцем, а не дериває його сам (PLAN §12).
        rows = [
            _client_row(client, template, sync_session, doc_host_path=doc_host_path)
            for client in repo.list_all()
        ]
        return PromptClientsDTO(
            template_key=template.key, generated_at=datetime.now(UTC), rows=rows
        )

    return await session.run_sync(_read)


@router.get(
    "/{key}/source",
    response_model=PromptSourceDTO,
    responses={404: {"model": ErrorDTO}},
)
async def get_source(key: str, settings: SettingsDep) -> PromptSourceDTO:
    template = get_template(key)
    if template is None:
        raise HTTPException(
            status_code=404,
            detail=_error("template_not_found", f"No prompt template named {key!r}."),
        )

    def _read() -> PromptSourceDTO | None:
        # `key` завжди зі свого замкненого реєстру (жодного шляху від
        # клієнта), але `safe_resolve` лишається — той самий подвійний
        # захист, що в `tools_strategy_package.py` (PathTraversalError).
        try:
            path: Path = safe_resolve(settings.aiseotools_source_path, template.doc_relpath)
        except PathTraversalError:
            return None
        if not path.is_file():
            return None
        raw = path.read_bytes()
        truncated = len(raw) > _SOURCE_MAX_BYTES
        text = raw[:_SOURCE_MAX_BYTES].decode("utf-8", errors="replace")
        return PromptSourceDTO(text=text, truncated=truncated)

    dto = await run_in_threadpool(_read)
    if dto is None:
        raise HTTPException(
            status_code=404,
            detail=_error(
                "prompt_file_not_found", f"Prompt file for {key!r} is not available."
            ),
        )
    return dto


register_router(router)
