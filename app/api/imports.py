"""Controlled bridge to the locally mounted AISeoTools checkout.

The browser never supplies a filesystem path.  The source is the fixed,
server-side ``Settings.aiseotools_source_path`` mount, and the importer is limited
to domains already present in the selected dashboard roster.  This keeps a
refresh from creating unrelated clients found elsewhere in AISeoTools/out.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.concurrency import run_in_threadpool

from app.api import register_router
from app.core.config import get_settings
from app.db.repositories.clients import ClientRepository
from app.db.session import get_sync_sessionmaker, worker_session_scope
from app.importers.ai_seo_tools import run_import
from app.integrations.aiseotools_sync import sync_ai_seo_tools_integrations
from app.schemas.api import (
    AISeoToolsImportRequest,
    AISeoToolsImportResponse,
    ErrorDTO,
)

router = APIRouter(prefix="/api/v1", tags=["imports"])


class ClientsNotFoundError(Exception):
    def __init__(self, client_ids: list[str]) -> None:
        super().__init__("one or more clients do not exist")
        self.client_ids = client_ids


def _selected_domains(body: AISeoToolsImportRequest) -> set[str]:
    with worker_session_scope() as session:
        clients = ClientRepository(session)
        if body.client_ids == "all":
            return {client.domain for client in clients.list_active()}
        selected = [clients.get(client_id) for client_id in body.client_ids]
        missing = [
            client_id
            for client_id, client in zip(body.client_ids, selected, strict=True)
            if client is None
        ]
        if missing:
            raise ClientsNotFoundError(missing)
        return {client.domain for client in selected if client is not None}


def _run_import(body: AISeoToolsImportRequest) -> AISeoToolsImportResponse:
    settings = get_settings()
    source = settings.aiseotools_source_path
    if not source.is_dir():
        return AISeoToolsImportResponse(source_available=False, dry_run=body.dry_run)

    allowed_domains = _selected_domains(body)
    report = run_import(
        source=source,
        dry_run=body.dry_run,
        session_factory=get_sync_sessionmaker(),
        allowed_domains=allowed_domains,
    )
    integration_report = sync_ai_seo_tools_integrations(
        session_factory=get_sync_sessionmaker(), allowed_domains=allowed_domains
    )
    outcomes: dict[str, int] = {}
    for artifact in report.artifacts:
        outcomes[artifact.outcome] = outcomes.get(artifact.outcome, 0) + 1
    return AISeoToolsImportResponse(
        source_available=True,
        dry_run=body.dry_run,
        scanned_artifacts=len(report.artifacts),
        imported_artifacts=report.imported_count,
        integrations_configured=integration_report.configured,
        outcomes=outcomes,
        warnings=integration_report.warnings,
    )


@router.post(
    "/imports/ai-seo-tools",
    response_model=AISeoToolsImportResponse,
    responses={404: {"model": ErrorDTO}, 503: {"model": ErrorDTO}},
)
async def import_ai_seo_tools(body: AISeoToolsImportRequest) -> AISeoToolsImportResponse:
    try:
        result = await run_in_threadpool(_run_import, body)
    except ClientsNotFoundError as exc:
        raise HTTPException(
            status_code=404,
            detail=ErrorDTO(
                error_code="client_not_found",
                message="one or more client_ids do not exist",
                details={"client_ids": exc.client_ids},
            ).model_dump(),
        ) from exc
    if not result.source_available:
        raise HTTPException(
            status_code=503,
            detail=ErrorDTO(
                error_code="aiseotools_unavailable",
                message="AISeoTools checkout is not mounted on the server",
            ).model_dump(),
        )
    return result


register_router(router)
