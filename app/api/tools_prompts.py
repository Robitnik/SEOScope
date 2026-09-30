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
from app.integrations.google_api import open_google_api_session
from app.integrations.google_api.drive_client import DriveClient
from app.integrations.google_api.sheets_client import SheetsClient
from app.prompts.registry import TEMPLATES, PromptTemplate, TemplateKind, get_template
from app.prompts.render import render_for_client, resolve_client
from app.prompts.strategy_render import BLOG_PREFILLABLE, PAGE_PREFILLABLE, build_rows
from app.prompts.strategy_rows import ParsedSheet, parse_sheet
from app.prompts.strategy_source import (
    GoogleClients,
    StrategySourceError,
    list_local_strategies,
    load_strategy,
)
from app.schemas.api import ErrorDTO
from app.schemas.enums import ClientStatus
from app.schemas.prompts import (
    PromptClientRowDTO,
    PromptClientsDTO,
    PromptFieldDTO,
    PromptLocalStrategiesDTO,
    PromptLocalStrategyDTO,
    PromptSourceDTO,
    PromptStrategyClientDTO,
    PromptStrategyDTO,
    PromptStrategyRequestDTO,
    PromptStrategyRowDTO,
    PromptStrategySheetDTO,
    PromptStrategySourceDTO,
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
        kind=template.kind.value,
        strategy_role=template.strategy_role,
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
    if template.kind is not TemplateKind.client:
        raise HTTPException(
            status_code=400,
            detail=_error(
                "wrong_template_kind",
                f"Template {key!r} is filled from a strategy sheet, not from the client list.",
            ),
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


# ── Шаблони зі стратегії (task-102) ──────────────────────────────────────────


@dataclass(frozen=True)
class _Clients:
    drive: DriveClient
    sheets: SheetsClient


def _open_clients() -> GoogleClients:
    """Одна Google-сесія на запит. Окрема функція модуля — щоб тести підміняли
    її фейком, як `tools_strategy_package._open_clients` (task-90)."""
    session = open_google_api_session()
    return _Clients(drive=DriveClient(session), sheets=SheetsClient(session))


@router.get("/strategy/local", response_model=PromptLocalStrategiesDTO)
async def local_strategies(settings: SettingsDep) -> PromptLocalStrategiesDTO:
    files = await run_in_threadpool(list_local_strategies, settings)
    return PromptLocalStrategiesDTO(
        files=[
            PromptLocalStrategyDTO(
                path_host=f.path_host, name=f.name, modified_at=f.modified_at, size=f.size
            )
            for f in files
        ]
    )


def _sheet_selectable(template: PromptTemplate, parsed: ParsedSheet) -> bool:
    return parsed.kind == template.strategy_role and parsed.role == template.strategy_role


@router.post(
    "/{key}/strategy",
    response_model=PromptStrategyDTO,
    responses={400: {"model": ErrorDTO}, 404: {"model": ErrorDTO}, 502: {"model": ErrorDTO}},
)
async def load_strategy_rows(
    key: str, body: PromptStrategyRequestDTO, session: SessionDep, settings: SettingsDep
) -> PromptStrategyDTO:
    template = get_template(key)
    if template is None:
        raise HTTPException(
            status_code=404,
            detail=_error("template_not_found", f"No prompt template named {key!r}."),
        )
    if template.kind is not TemplateKind.strategy:
        raise HTTPException(
            status_code=400,
            detail=_error(
                "wrong_template_kind",
                f"Template {key!r} is filled from the client list, not from a strategy sheet.",
            ),
        )
    try:
        loaded = await run_in_threadpool(
            lambda: load_strategy(body.source, settings=settings, open_clients=_open_clients)
        )
    except StrategySourceError as exc:
        raise HTTPException(
            status_code=exc.status, detail=_error(exc.code, exc.message)
        ) from exc

    parsed_sheets = [parse_sheet(raw) for raw in loaded.sheets]
    if body.sheet is not None:
        wanted = next((p for p in parsed_sheets if p.name == body.sheet), None)
        if wanted is None:
            raise HTTPException(
                status_code=400,
                detail=_error("unknown_sheet", f"Sheet not found in this file: {body.sheet!r}."),
            )
        if not _sheet_selectable(template, wanted):
            raise HTTPException(
                status_code=400,
                detail=_error(
                    "sheet_not_selectable",
                    f"Sheet {body.sheet!r} has no {template.strategy_role} rows "
                    f"for {template.name}.",
                ),
            )
        chosen: ParsedSheet | None = wanted
    else:
        chosen = next((p for p in parsed_sheets if _sheet_selectable(template, p)), None)

    doc_host_path = _host_doc_path(settings, template)
    prefillable = list(PAGE_PREFILLABLE if template.strategy_role == "page" else BLOG_PREFILLABLE)

    def _build(sync_session: Session) -> PromptStrategyDTO:
        warnings: list[str] = []
        rows: list[PromptStrategyRowDTO] = []
        client_dto: PromptStrategyClientDTO | None = None
        if chosen is not None:
            urls: list[str] = []
            for page in chosen.pages:
                urls.extend(u for u in (page.url, page.new_url) if u)
            for article in chosen.articles:
                urls.extend(article.internal_links)
                if article.commercial:
                    urls.append(article.commercial)
            resolved = resolve_client(
                sync_session, file_name=loaded.name, urls=urls, client_id=body.client_id
            )
            if resolved.client is not None and resolved.label is not None:
                client_dto = PromptStrategyClientDTO(
                    id=resolved.client.id,
                    name=resolved.client.name,
                    label=resolved.label,
                    resolved_by=resolved.resolved_by,  # type: ignore[arg-type]
                )
            built = build_rows(
                template,
                chosen,
                client_label=resolved.label,
                doc_host_path=doc_host_path,
                today=datetime.now(UTC).date(),
            )
            rows = [PromptStrategyRowDTO(**vars(r)) for r in built]
            if chosen.orphan_rows:
                warnings.append(
                    f"{chosen.orphan_rows} row(s) with keywords or links before the first "
                    "article were skipped."
                )
        return PromptStrategyDTO(
            template_key=template.key,
            generated_at=datetime.now(UTC),
            source=PromptStrategySourceDTO(
                name=loaded.name,
                kind=loaded.kind,
                ref=loaded.ref,
                modified_time=loaded.modified_time,
                web_view_link=loaded.web_view_link,
            ),
            client=client_dto,
            sheets=[
                PromptStrategySheetDTO(
                    name=p.name,
                    role=p.role,
                    selectable=_sheet_selectable(template, p),
                    row_count=p.row_count,
                )
                for p in parsed_sheets
            ],
            sheet=chosen.name if chosen is not None else None,
            prefillable=prefillable,
            rows=rows,
            warnings=warnings,
        )

    return await session.run_sync(_build)


register_router(router)
