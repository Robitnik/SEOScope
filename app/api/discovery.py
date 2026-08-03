"""Auto-connect endpoint for one `(client, provider)` pair (task-33; PLAN §10).

`POST /api/v1/clients/{client_id}/integrations/{provider}/discover` replaces the
manual "type the account id yourself" dialog: it asks the provider which account
matches the client's domain, writes the confident match, and reports what it did.

Like every mutating router here, this one owns its commit — `get_db` hands out a
bare `AsyncSession` with no auto-commit-on-exit, so a repository flush alone
would be rolled back when the request ends.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.concurrency import run_in_threadpool
from sqlalchemy.orm import Session

from app.api import register_router
from app.api.deps import SessionDep
from app.db.models import Client, ClientIntegration
from app.db.repositories.clients import ClientRepository
from app.integrations.discovery import DiscoveryOutcome, IntegrationCandidate, discover
from app.integrations.registry import get_adapter
from app.metrics import resolve_periods
from app.schemas.adapter import CollectionContext, PreflightResult
from app.schemas.api import (
    ErrorDTO,
    IntegrationCandidateDTO,
    IntegrationDiscoverRequest,
    IntegrationDiscoveryDTO,
    IntegrationDTO,
)
from app.schemas.enums import METRIC_PROVIDERS, ComparisonMode, PeriodKind, Provider

router = APIRouter(prefix="/api/v1", tags=["clients"])

_NOT_FOUND = ErrorDTO(error_code="client_not_found", message="Client not found").model_dump()


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


def _to_candidate_dto(candidate: IntegrationCandidate) -> IntegrationCandidateDTO:
    return IntegrationCandidateDTO(
        label=candidate.label,
        detail=candidate.detail,
        confidence=candidate.confidence,
        reason=candidate.reason,
        source_mode=candidate.source_mode,
        integration_config=dict(candidate.integration_config),
        suggested=candidate.suggested,
    )


def _run_preflight(client: Client, integration: ClientIntegration) -> PreflightResult:
    """Probe the integration we have just written.

    Mirrors `app/api/clients.py`'s preflight endpoint: a synthetic closed-week
    context is enough to answer "is this reference usable at all".
    """
    provider = integration.provider
    periods = resolve_periods(
        PeriodKind.week,
        datetime.now(UTC),
        tz=client.timezone,
        mode=ComparisonMode.closed,
    )
    context = CollectionContext(
        client_id=client.id,
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


@router.post(
    "/clients/{client_id}/integrations/{provider}/discover",
    response_model=IntegrationDiscoveryDTO,
    responses={404: {"model": ErrorDTO}, 400: {"model": ErrorDTO}},
)
async def discover_integration(
    client_id: str,
    provider: Provider,
    body: IntegrationDiscoverRequest,
    session: SessionDep,
) -> IntegrationDiscoveryDTO:
    if provider not in METRIC_PROVIDERS:
        # `tech_audit`/`blogs` are machine-only collectors with no account to
        # look up — expanding to `tuple(Provider)` here would offer a Connect
        # button for something that cannot be connected.
        raise HTTPException(
            status_code=400,
            detail=ErrorDTO(
                error_code="provider_not_connectable",
                message=f"{provider.value} has no integration to discover",
            ).model_dump(),
        )

    def _load(s: Session) -> tuple[Client, ClientIntegration | None] | None:
        clients = ClientRepository(s)
        client = clients.get(client_id)
        if client is None:
            return None
        return client, clients.get_integration(client_id, provider)

    loaded = await session.run_sync(_load)
    if loaded is None:
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    client, existing = loaded

    if existing is not None and existing.enabled:
        return IntegrationDiscoveryDTO(
            client_id=client_id,
            provider=provider,
            status="already_configured",
            message="This integration is already connected.",
            applied=_to_integration_dto(existing),
        )

    # Blocking HTTP against Reporting Ninja, not DB work — `run_in_threadpool`,
    # matching the pattern documented in `app/api/providers.py`.
    outcome: DiscoveryOutcome = await run_in_threadpool(
        discover,
        provider,
        domain=client.domain,
        client_name=client.name,
        market=client.market,
    )

    candidates = [_to_candidate_dto(candidate) for candidate in outcome.candidates]

    if outcome.auto_apply is None or not body.apply:
        # `needs_choice`, `not_found`, `unavailable` — and any dry run — write
        # nothing at all. A dry run that *did* find a confident match still
        # reports `needs_choice`, so the message has to say "confirm", not
        # "matched", or the UI would claim a connection that never happened.
        if outcome.auto_apply is not None:
            return IntegrationDiscoveryDTO(
                client_id=client_id,
                provider=provider,
                status="needs_choice",
                message=f"Found {outcome.auto_apply.label}. Confirm to connect.",
                candidates=candidates,
            )
        return IntegrationDiscoveryDTO(
            client_id=client_id,
            provider=provider,
            status=outcome.status,
            message=outcome.message,
            candidates=candidates,
        )

    chosen = outcome.auto_apply
    # `upsert_integration` REPLACES `config_json` wholesale, so merge here or an
    # operator-set key (`unit_budget`, `csv_dir`) would be silently dropped when
    # re-connecting a disabled integration.
    merged: dict[str, Any] = dict(existing.config_json or {}) if existing is not None else {}
    merged.update(chosen.integration_config)

    def _apply(s: Session) -> ClientIntegration:
        return ClientRepository(s).upsert_integration(
            client_id,
            provider,
            enabled=True,
            source_mode=chosen.source_mode,
            config=merged,
        )

    integration = await session.run_sync(_apply)
    await session.commit()

    preflight = await run_in_threadpool(_run_preflight, client, integration)
    message = outcome.message if preflight.ok else f"{outcome.message} Verification failed."
    return IntegrationDiscoveryDTO(
        client_id=client_id,
        provider=provider,
        status="connected",
        message=message,
        applied=_to_integration_dto(integration),
        candidates=candidates,
        preflight=preflight,
    )


register_router(router)
