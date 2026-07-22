"""Clients & integrations endpoints (PLAN §10/§13): CRUD, safe logo upload,
and per-`(client, provider)` integration config + preflight.

Every mutating endpoint here owns its own commit: `app.db.session.get_db`
hands out a bare `AsyncSession` with no auto-commit-on-exit, so a write that
only flushes (as every repository method does) would otherwise be silently
rolled back when the request ends.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api import register_router
from app.api.deps import PaginationDep, SessionDep, SettingsDep
from app.core.security import PathTraversalError, safe_resolve
from app.db.models import Client, ClientIntegration
from app.db.repositories.clients import ClientRepository
from app.integrations.registry import get_adapter
from app.metrics import resolve_periods
from app.schemas.adapter import CollectionContext, PreflightResult
from app.schemas.api import (
    ClientCreateRequest,
    ClientDTO,
    ClientUpdateRequest,
    ErrorDTO,
    IntegrationDTO,
    IntegrationUpsertRequest,
)
from app.schemas.enums import ComparisonMode, PeriodKind, Provider

router = APIRouter(prefix="/api/v1", tags=["clients"])

_MAX_LOGO_BYTES = 2 * 1024 * 1024
_MIN_LOGO_DIMENSION = 16
_MAX_LOGO_DIMENSION = 4096
_LOGO_EXTENSIONS = ("png", "jpg")

_NOT_FOUND = ErrorDTO(error_code="client_not_found", message="Client not found").model_dump()
_LOGO_NOT_FOUND = ErrorDTO(
    error_code="logo_not_found", message="No logo for this client"
).model_dump()


class InvalidLogoError(ValueError):
    """Raised for any logo that fails MIME/signature/size/dimension checks."""


def _sniff_png(data: bytes) -> tuple[int, int]:
    # 8-byte signature + 4-byte length + 4-byte "IHDR" + 4-byte width + 4-byte height.
    if len(data) < 24 or data[12:16] != b"IHDR":
        raise InvalidLogoError("malformed PNG: missing IHDR chunk")
    width = int.from_bytes(data[16:20], "big")
    height = int.from_bytes(data[20:24], "big")
    return width, height


_JPEG_SOF_MARKERS = frozenset(
    {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
)
_JPEG_NO_LENGTH_MARKERS = frozenset({0xD8, 0xD9, 0x01}) | frozenset(range(0xD0, 0xD8))


def _sniff_jpeg(data: bytes) -> tuple[int, int]:
    pos = 2
    while pos + 1 < len(data):
        if data[pos] != 0xFF:
            raise InvalidLogoError("malformed JPEG: expected marker")
        marker = data[pos + 1]
        if marker in _JPEG_NO_LENGTH_MARKERS:
            pos += 2
            continue
        if pos + 4 > len(data):
            break
        length = int.from_bytes(data[pos + 2 : pos + 4], "big")
        if marker in _JPEG_SOF_MARKERS:
            if pos + 9 > len(data):
                break
            height = int.from_bytes(data[pos + 5 : pos + 7], "big")
            width = int.from_bytes(data[pos + 7 : pos + 9], "big")
            return width, height
        pos += 2 + length
    raise InvalidLogoError("malformed JPEG: no SOF segment found")


def _sniff_image(data: bytes) -> tuple[str, int, int]:
    """Validate signature and extract dimensions without a native image lib.

    Only PNG/JPEG are accepted for the MVP (PLAN §13 "MIME/signature/size/
    dimensions validation") — SVG is deliberately excluded (XML, XXE-prone,
    "dimensions" aren't well-defined without a full parser).
    """
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        width, height = _sniff_png(data)
        return "image/png", width, height
    if data[:3] == b"\xff\xd8\xff":
        width, height = _sniff_jpeg(data)
        return "image/jpeg", width, height
    raise InvalidLogoError("unsupported image format; only PNG and JPEG are accepted")


def _logo_url(client: Client) -> str | None:
    return f"/api/v1/clients/{client.id}/logo" if client.logo_path else None


def _to_client_dto(client: Client) -> ClientDTO:
    return ClientDTO(
        id=client.id,
        name=client.name,
        slug=client.slug,
        domain=client.domain,
        logo_path=_logo_url(client),
        market=client.market,
        timezone=client.timezone,
        enabled=client.enabled,
        created_at=client.created_at,
        updated_at=client.updated_at,
    )


def _to_integration_dto(integration: ClientIntegration) -> IntegrationDTO:
    return IntegrationDTO(
        id=integration.id,
        client_id=integration.client_id,
        provider=integration.provider,
        enabled=integration.enabled,
        source_mode=integration.source_mode,
        integration_config=dict(integration.config_json or {}),
        last_success_at=integration.last_success_at,
        last_attempt_at=integration.last_attempt_at,
    )


@router.get("/clients", response_model=list[ClientDTO])
async def list_clients(session: SessionDep, page: PaginationDep) -> list[ClientDTO]:
    def _run(s: Session) -> list[Client]:
        return ClientRepository(s).list_all()[page.offset : page.offset + page.limit]

    clients = await session.run_sync(_run)
    return [_to_client_dto(c) for c in clients]


@router.post("/clients", response_model=ClientDTO, status_code=201)
async def create_client(body: ClientCreateRequest, session: SessionDep) -> ClientDTO:
    def _run(s: Session) -> Client:
        return ClientRepository(s).create(
            name=body.name,
            slug=body.slug,
            domain=body.domain,
            market=body.market,
            timezone=body.timezone,
            enabled=body.enabled,
        )

    try:
        client = await session.run_sync(_run)
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=409,
            detail=ErrorDTO(
                error_code="client_already_exists", message="slug or domain already in use"
            ).model_dump(),
        ) from exc
    return _to_client_dto(client)


@router.get(
    "/clients/{client_id}", response_model=ClientDTO, responses={404: {"model": ErrorDTO}}
)
async def get_client(client_id: str, session: SessionDep) -> ClientDTO:
    client = await session.run_sync(lambda s: ClientRepository(s).get(client_id))
    if client is None:
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    return _to_client_dto(client)


@router.patch(
    "/clients/{client_id}", response_model=ClientDTO, responses={404: {"model": ErrorDTO}}
)
async def update_client(
    client_id: str, body: ClientUpdateRequest, session: SessionDep
) -> ClientDTO:
    fields: dict[str, Any] = body.model_dump(exclude_unset=True)

    def _run(s: Session) -> Client | None:
        if ClientRepository(s).get(client_id) is None:
            return None
        return ClientRepository(s).update(client_id, **fields)

    try:
        client = await session.run_sync(_run)
        if client is None:
            raise HTTPException(status_code=404, detail=_NOT_FOUND)
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=409,
            detail=ErrorDTO(
                error_code="client_conflict", message="domain already in use"
            ).model_dump(),
        ) from exc
    return _to_client_dto(client)


@router.post(
    "/clients/{client_id}/logo", response_model=ClientDTO, responses={404: {"model": ErrorDTO}}
)
async def upload_logo(
    client_id: str, request: Request, session: SessionDep, settings: SettingsDep
) -> ClientDTO:
    """Accepts the raw image bytes as the request body (`Content-Type:
    image/png` or `image/jpeg`) rather than a `multipart/form-data` upload —
    a logo is a single file with no other form fields, so there is nothing
    multipart buys here, and it keeps this endpoint free of the extra
    `python-multipart` dependency `UploadFile` would otherwise require.
    """
    client = await session.run_sync(lambda s: ClientRepository(s).get(client_id))
    if client is None:
        raise HTTPException(status_code=404, detail=_NOT_FOUND)

    contents = await request.body()
    if len(contents) > _MAX_LOGO_BYTES:
        raise HTTPException(
            status_code=400,
            detail=ErrorDTO(
                error_code="logo_too_large",
                message=f"logo exceeds {_MAX_LOGO_BYTES} bytes",
            ).model_dump(),
        )

    try:
        mime, width, height = _sniff_image(contents)
    except InvalidLogoError as exc:
        raise HTTPException(
            status_code=400,
            detail=ErrorDTO(error_code="invalid_logo", message=str(exc)).model_dump(),
        ) from exc
    if not (_MIN_LOGO_DIMENSION <= width <= _MAX_LOGO_DIMENSION) or not (
        _MIN_LOGO_DIMENSION <= height <= _MAX_LOGO_DIMENSION
    ):
        raise HTTPException(
            status_code=400,
            detail=ErrorDTO(
                error_code="invalid_logo_dimensions",
                message=f"logo must be between {_MIN_LOGO_DIMENSION} and "
                f"{_MAX_LOGO_DIMENSION}px on each side",
            ).model_dump(),
        )

    ext = "png" if mime == "image/png" else "jpg"
    # Fixed, generated filename keyed only by the client's own id — never
    # derived from the uploaded filename, so there is nothing to sanitize
    # past this point (PLAN §13 "generated safe filename").
    logo_rel_path = f"logos/{client_id}.{ext}"
    target = safe_resolve(settings.artifacts_dir, logo_rel_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    for stale_ext in _LOGO_EXTENSIONS:
        if stale_ext == ext:
            continue
        stale = safe_resolve(settings.artifacts_dir, f"logos/{client_id}.{stale_ext}")
        stale.unlink(missing_ok=True)
    target.write_bytes(contents)

    updated = await session.run_sync(
        lambda s: ClientRepository(s).set_logo(client_id, logo_rel_path)
    )
    await session.commit()
    return _to_client_dto(updated)


@router.get("/clients/{client_id}/logo", responses={404: {"model": ErrorDTO}})
async def get_logo(client_id: str, session: SessionDep, settings: SettingsDep) -> FileResponse:
    client = await session.run_sync(lambda s: ClientRepository(s).get(client_id))
    if client is None or not client.logo_path:
        raise HTTPException(status_code=404, detail=_LOGO_NOT_FOUND)
    try:
        resolved = safe_resolve(settings.artifacts_dir, client.logo_path)
    except PathTraversalError as exc:
        raise HTTPException(status_code=404, detail=_LOGO_NOT_FOUND) from exc
    if not resolved.is_file():
        raise HTTPException(status_code=404, detail=_LOGO_NOT_FOUND)
    media_type = "image/png" if resolved.suffix == ".png" else "image/jpeg"
    return FileResponse(resolved, media_type=media_type)


@router.put(
    "/clients/{client_id}/integrations/{provider}",
    response_model=IntegrationDTO,
    responses={404: {"model": ErrorDTO}},
)
async def upsert_integration(
    client_id: str,
    provider: Provider,
    body: IntegrationUpsertRequest,
    session: SessionDep,
) -> IntegrationDTO:
    def _run(s: Session) -> ClientIntegration | None:
        if ClientRepository(s).get(client_id) is None:
            return None
        return ClientRepository(s).upsert_integration(
            client_id,
            provider,
            enabled=body.enabled,
            source_mode=body.source_mode,
            config=body.integration_config,
        )

    integration = await session.run_sync(_run)
    if integration is None:
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    await session.commit()
    return _to_integration_dto(integration)


@router.post(
    "/clients/{client_id}/integrations/{provider}/preflight",
    response_model=PreflightResult,
    responses={404: {"model": ErrorDTO}},
)
async def preflight_integration(
    client_id: str, provider: Provider, session: SessionDep
) -> PreflightResult:
    def _run(s: Session) -> tuple[Client, ClientIntegration | None] | None:
        client = ClientRepository(s).get(client_id)
        if client is None:
            return None
        return client, ClientRepository(s).get_integration(client_id, provider)

    result = await session.run_sync(_run)
    if result is None:
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    client, integration = result

    if integration is None or not integration.enabled:
        return PreflightResult(provider=provider, ok=False, reason_code="not_configured")

    periods = resolve_periods(
        PeriodKind.week,
        datetime.now(UTC),
        tz=client.timezone,
        mode=ComparisonMode.closed,
    )
    context = CollectionContext(
        client_id=client_id,
        provider=provider,
        source_mode=integration.source_mode,
        period_kind=PeriodKind.week,
        comparison_mode=ComparisonMode.closed,
        current=periods.current,
        previous=periods.previous,
        timezone=client.timezone,
        integration_config=dict(integration.config_json or {}),
    )
    try:
        adapter = get_adapter(provider)
    except LookupError:
        return PreflightResult(
            provider=provider,
            ok=False,
            reason_code="adapter_unavailable",
            message="no provider adapter registered yet",
        )
    try:
        return adapter.preflight(context)
    except Exception:  # noqa: BLE001 - preflight must never crash the request (PLAN §9.2)
        return PreflightResult(
            provider=provider, ok=False, reason_code="preflight_error", message="preflight failed"
        )


register_router(router)
