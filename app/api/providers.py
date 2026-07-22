"""`GET /api/v1/providers/status` (PLAN §10/§13): every `(client, provider)`
cell's configured/auth/preflight state, without ever exposing a secret.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, HTTPException
from fastapi.concurrency import run_in_threadpool
from sqlalchemy.orm import Session

from app.api import register_router
from app.api.deps import SessionDep
from app.db.models import SyncJobProvider
from app.db.repositories.cells import CellRepository
from app.db.repositories.clients import ClientRepository
from app.db.repositories.snapshots import SnapshotRepository
from app.integrations.browser_session import NoWarmChrome, check_session, start_login
from app.jobs.lane import DEFAULT_AUTH_PAUSE_TIMEOUT_SECONDS
from app.schemas.api import (
    ErrorDTO,
    ProviderLoginDTO,
    ProviderSessionDTO,
    ProvidersStatusDTO,
    ProviderStatusCellDTO,
)
from app.schemas.enums import CellStatus, Provider

router = APIRouter(prefix="/api/v1", tags=["providers"])


def _auth_state_from_cell(
    latest_cell: SyncJobProvider | None,
) -> Literal["auth_required", "failed"] | None:
    """task-21: mirrors `app.api.dashboard._auth_state_from_cell` — kept as a
    separate copy since the two routers share no common module, matching this
    file's existing style of not cross-importing between API modules.
    """
    if latest_cell is None:
        return None
    if latest_cell.status == CellStatus.awaiting_auth:
        return "auth_required"
    if latest_cell.status in (CellStatus.failed, CellStatus.failed_interrupted):
        return "auth_required" if latest_cell.error_code == "auth_required" else "failed"
    return None


def _build_status_cell(
    session: Session, client_id: str, provider: Provider
) -> ProviderStatusCellDTO:
    integration = ClientRepository(session).get_integration(client_id, provider)
    if integration is None or not integration.enabled:
        return ProviderStatusCellDTO(
            client_id=client_id,
            provider=provider,
            configured=False,
            state="not_configured",
            reason_code="not_configured",
        )

    latest_cell = CellRepository(session).latest_for_client_provider(client_id, provider)
    current_snapshot = SnapshotRepository(session).latest_success(client_id, provider)

    auth_state = _auth_state_from_cell(latest_cell)
    reason_code: str | None = None
    if current_snapshot is not None:
        # PLAN §12: last-successful data stays visible; only the badge flips.
        state = auth_state if auth_state == "auth_required" else current_snapshot.quality.value
        if auth_state == "auth_required":
            reason_code = latest_cell.error_code if latest_cell is not None else None
    elif auth_state is not None:
        reason_code = latest_cell.error_code if latest_cell is not None else None
        state = auth_state
    else:
        state = "unavailable"

    last_checked_at = integration.last_attempt_at
    if latest_cell is not None:
        last_checked_at = (
            latest_cell.finished_at or latest_cell.started_at or latest_cell.created_at
        )

    return ProviderStatusCellDTO(
        client_id=client_id,
        provider=provider,
        configured=True,
        source_mode=integration.source_mode,
        state=state,  # type: ignore[arg-type]
        reason_code=reason_code,
        units_estimated=latest_cell.units_estimated if latest_cell is not None else None,
        last_checked_at=last_checked_at,
    )


@router.get("/providers/status", response_model=ProvidersStatusDTO)
async def get_providers_status(session: SessionDep) -> ProvidersStatusDTO:
    def _run(s: Session) -> list[ProviderStatusCellDTO]:
        clients = ClientRepository(s).list_all()
        return [
            _build_status_cell(s, client.id, provider)
            for client in clients
            for provider in Provider
        ]

    cells = await session.run_sync(_run)
    return ProvidersStatusDTO(generated_at=datetime.now(UTC), cells=cells)


# --- Browser session bridge (task-20 primitives, task-22 HTTP surface) -----
#
# `check_session`/`start_login` are blocking Playwright/CDP calls, not DB
# work — `run_in_threadpool`, matching `app/api/jobs.py`'s existing pattern
# for its own blocking (non-session) work, not `session.run_sync`.

_PROVIDER_HAS_NO_SESSION = "provider_has_no_session"
_NO_WARM_CHROME = "no_warm_chrome"


@router.post(
    "/providers/{provider}/login",
    response_model=ProviderLoginDTO,
    responses={400: {"model": ErrorDTO}, 503: {"model": ErrorDTO}},
)
async def start_provider_login(provider: Provider) -> ProviderLoginDTO:
    try:
        login_session_id = await run_in_threadpool(start_login, provider)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=ErrorDTO(error_code=_PROVIDER_HAS_NO_SESSION, message=str(exc)).model_dump(),
        ) from exc
    except NoWarmChrome as exc:
        raise HTTPException(
            status_code=503,
            detail=ErrorDTO(error_code=_NO_WARM_CHROME, message=exc.remedy).model_dump(),
        ) from exc
    # `start_login` only navigates the login page open — it does not itself
    # know whether the operator has finished signing in yet (usually not,
    # this soon). Report the real, freshly-probed state rather than assuming.
    state = await run_in_threadpool(check_session, provider)
    return ProviderLoginDTO(
        login_session_id=login_session_id,
        state=state.value,
        timeout_seconds=int(DEFAULT_AUTH_PAUSE_TIMEOUT_SECONDS),
    )


@router.get(
    "/providers/{provider}/session",
    response_model=ProviderSessionDTO,
    responses={400: {"model": ErrorDTO}},
)
async def get_provider_session(provider: Provider) -> ProviderSessionDTO:
    try:
        state = await run_in_threadpool(check_session, provider)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=ErrorDTO(error_code=_PROVIDER_HAS_NO_SESSION, message=str(exc)).model_dump(),
        ) from exc
    return ProviderSessionDTO(state=state.value)


register_router(router)
