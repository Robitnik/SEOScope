"""Monthly Performance table REST API (task-54), prefix
`/api/v1/clients/{client_id}/monthly-table`.

A NEW router module, not an addition to `app/api/reports.py` (task-54 owns
this file exclusively) — `app/main.py` needs no edit either, routers
self-register via `app.api.register_router` and are auto-discovered
(`app/main.py::_discover_routers`).

Deliberately synchronous, not a `SyncJob`/`ReportRun` (task-54 §2.5): a full
refresh is 3-4 Reporting Ninja requests, measured at ~2-4s per client — a
queue with lease/heartbeat/SSE would cost more than it buys. `/refresh`
without `force` only ever queries the un-frozen TAIL of the window (the
oldest months are already frozen and read straight from the DB), which is
what keeps repeat calls cheap.
"""

from __future__ import annotations

import os
from datetime import date
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse

from app.api import register_router
from app.core.config import get_settings
from app.db.models import ClientMonthlyMetric
from app.db.repositories.client_monthly import ClientMonthlyRepository
from app.db.repositories.clients import ClientRepository
from app.db.session import worker_session_scope
from app.reports import monthly_table, monthly_xlsx
from app.reports.api_source import discover_gsc_fields
from app.reports.history import month_keys
from app.reports.ninja_api import ReportingNinjaClient
from app.schemas.api import ErrorDTO
from app.schemas.client_monthly import (
    AttributionChangedDetailDTO,
    KeyEventCandidateDTO,
    KeyEventCandidatesDTO,
    MonthlyRowDTO,
    MonthlyTableCoverageDTO,
    MonthlyTableDTO,
    MonthlyTableRefreshResponseDTO,
    MonthlyTableSettingsDTO,
    MonthlyTableSettingsUpdateRequest,
)
from app.schemas.enums import Provider

router = APIRouter(
    prefix="/api/v1/clients/{client_id}/monthly-table", tags=["client-monthly-table"]
)

_CLIENT_NOT_FOUND = ErrorDTO(error_code="client_not_found", message="Client not found").model_dump()
_NOT_CONFIGURED = ErrorDTO(
    error_code="not_configured",
    message="This client has no Reporting Ninja integration — nothing to collect.",
).model_dump()
_DEFAULT_ATTRIBUTION_USERS: monthly_table.Attribution = "first_user"
_DEFAULT_ATTRIBUTION_EVENTS: monthly_table.Attribution = "session"


class NotConfiguredError(Exception):
    pass


def _settings_dto(row: Any) -> MonthlyTableSettingsDTO:
    if row is None:
        return MonthlyTableSettingsDTO(
            key_event_names=[],
            key_event_label=None,
            attribution_users=_DEFAULT_ATTRIBUTION_USERS,
            attribution_events=_DEFAULT_ATTRIBUTION_EVENTS,
            history_months=None,
        )
    return MonthlyTableSettingsDTO(
        key_event_names=list(row.key_event_names_json or []),
        key_event_label=row.key_event_label,
        attribution_users=row.attribution_users,
        attribution_events=row.attribution_events,
        history_months=row.history_months,
    )


def _effective_months(row: Any, *, default_history_months: int) -> int:
    if row is not None and row.history_months is not None:
        return int(row.history_months)
    return default_history_months


def _built_monthly_rows(
    keys: list[str], stored: dict[str, ClientMonthlyMetric]
) -> list[monthly_table.MonthlyRow]:
    """Turn stored DB rows into `MonthlyRow`s with directions recomputed
    across the FULL requested window — `build_rows` needs every month in
    order to derive MoM direction for the first freshly-collected month
    against the last frozen one (task-54 §2.2), so this always runs over
    the whole window, not just newly-fetched months. Shared by the JSON
    table (`_build_rows` below, DTO-wrapped) and the XLSX export, which
    needs the same rows un-wrapped.
    """
    fetched: list[monthly_table.MonthFetch] = []
    days_in_month: dict[str, int] = {}
    for key in keys:
        row = stored.get(key)
        if row is None:
            fetched.append(
                monthly_table.MonthFetch(
                    month=key,
                    organic_users=None,
                    organic_conversions=None,
                    key_event_count=None,
                    gsc_clicks=None,
                    gsc_impressions=None,
                    gsc_days_with_data=None,
                    ga4_state="no_data",
                    gsc_state="no_data",
                    truncated=False,
                )
            )
            continue
        days_in_month[key] = row.days_in_month
        fetched.append(
            monthly_table.MonthFetch(
                month=row.month,
                organic_users=row.organic_users,
                organic_conversions=row.organic_conversions,
                key_event_count=row.key_event_count,
                gsc_clicks=row.gsc_clicks,
                gsc_impressions=row.gsc_impressions,
                gsc_days_with_data=row.gsc_days_with_data,
                ga4_state=row.ga4_state,  # type: ignore[arg-type]
                gsc_state=row.gsc_state,  # type: ignore[arg-type]
                truncated=row.truncated,
            )
        )
    return monthly_table.build_rows(fetched, days_in_month)


def _build_rows(keys: list[str], stored: dict[str, ClientMonthlyMetric]) -> list[MonthlyRowDTO]:
    built = _built_monthly_rows(keys, stored)
    return [
        MonthlyRowDTO(
            month=r.month,
            label=r.label,
            days_in_month=r.days_in_month,
            organic_users=r.organic_users,
            organic_conversions=r.organic_conversions,
            key_event_count=r.key_event_count,
            gsc_clicks=r.gsc_clicks,
            gsc_impressions=r.gsc_impressions,
            gsc_days_with_data=r.gsc_days_with_data,
            conversion_rate=r.conversion_rate,
            ctr=r.ctr,
            is_partial_period=r.is_partial_period,
            ga4_state=r.ga4_state,
            gsc_state=r.gsc_state,
            truncated=r.truncated,
            directions=dict(r.directions),
        )
        for r in built
    ]


def _get_table(client_id: str) -> MonthlyTableDTO | None:
    with worker_session_scope() as session:
        client = ClientRepository(session).get(client_id)
        if client is None:
            return None
        repo = ClientMonthlyRepository(session)
        settings_row = repo.get_settings(client_id)
        app_settings = get_settings()
        months = _effective_months(
            settings_row, default_history_months=app_settings.client_monthly_history_months
        )
        keys = month_keys(count=months)
        stored = repo.list_window(client_id, keys)
        rows = _build_rows(keys, stored)
        gsc_integration = ClientRepository(session).get_integration(client_id, Provider.gsc)
        has_key_event_column = bool(settings_row and settings_row.key_event_names_json)
        collected_ats = [row.collected_at for row in stored.values()]
        return MonthlyTableDTO(
            client_id=client_id,
            rows=rows,
            settings=_settings_dto(settings_row),
            coverage=MonthlyTableCoverageDTO(
                has_gsc=gsc_integration is not None,
                has_key_event_column=has_key_event_column,
                requested_months=len(keys),
                available_months=len(stored),
            ),
            collected_at=max(collected_ats).isoformat() if collected_ats else None,
        )


@router.get("", responses={404: {"model": ErrorDTO}})
async def get_monthly_table(client_id: str) -> MonthlyTableDTO:
    table = _get_table(client_id)
    if table is None:
        raise HTTPException(404, _CLIENT_NOT_FOUND)
    return table


def _ninja_refs(
    session: Any, client_id: str
) -> tuple[str, str, str | None, tuple[str, str] | None] | None:
    """`(connection_key, ga4_account_id, ninja_report_name, gsc_ref)` or
    `None` when the client has no `reporting_ninja` integration — the one
    provider this table cannot degrade without (task-54 §2.5 `409
    not_configured`), unlike GSC which degrades to `not_configured` per row.
    """
    client_repo = ClientRepository(session)
    rn_integration = client_repo.get_integration(client_id, Provider.reporting_ninja)
    if rn_integration is None or not rn_integration.config_json.get("connection_key"):
        return None
    connection_key = rn_integration.config_json["connection_key"]
    account_id = rn_integration.config_json["account_id"]
    report_name = rn_integration.config_json.get("ninja_report_name")
    gsc_integration = client_repo.get_integration(client_id, Provider.gsc)
    gsc_ref = None
    if gsc_integration is not None and gsc_integration.config_json.get("connection_key"):
        gsc_ref = (
            gsc_integration.config_json["connection_key"],
            gsc_integration.config_json["account_id"],
        )
    return connection_key, account_id, report_name, gsc_ref


def _refresh(client_id: str, *, force: str | None) -> MonthlyTableRefreshResponseDTO:
    app_settings = get_settings()
    api_key = os.environ.get("REPORTING_NINJA_API_KEY", "")
    with worker_session_scope() as session:
        client = ClientRepository(session).get(client_id)
        if client is None:
            raise NotConfiguredError("client_not_found")
        refs = _ninja_refs(session, client_id)
        if refs is None or not api_key:
            raise NotConfiguredError("not_configured")
        connection_key, ga4_account_id, _report_name, gsc_ref = refs

        repo = ClientMonthlyRepository(session)
        settings_row = repo.get_or_create_settings(client_id)
        history_months = _effective_months(
            settings_row, default_history_months=app_settings.client_monthly_history_months
        )
        keys = month_keys(count=history_months)
        to_collect = repo.months_needing_collection(client_id, keys, force=force)

        requests_used = 0
        if to_collect:
            with ReportingNinjaClient(api_key) as client_rn:
                ga4_fields = monthly_table.discover_ga4_monthly_fields(client_rn)
                gsc_fields = discover_gsc_fields(client_rn) if gsc_ref is not None else None
                result = monthly_table.fetch_months(
                    client_rn,
                    ga4_connection_key=connection_key,
                    ga4_account_id=ga4_account_id,
                    ga4_fields=ga4_fields,
                    gsc_ref=gsc_ref,
                    gsc_fields=gsc_fields,
                    keys=to_collect,
                    attribution_users=settings_row.attribution_users,  # type: ignore[arg-type]
                    attribution_events=settings_row.attribution_events,  # type: ignore[arg-type]
                    key_event_names=list(settings_row.key_event_names_json or []),
                )
                requests_used = result.requests_used
                today = date.today()
                for fetched in result.months:
                    repo.upsert_month(
                        client_id,
                        fetched,
                        days_in_month=monthly_table.days_in_month(fetched.month),
                        attribution_users=settings_row.attribution_users,
                        attribution_events=settings_row.attribution_events,
                        key_event_names=list(settings_row.key_event_names_json or []),
                        today=today,
                    )
        repo.commit()

    table = _get_table(client_id)
    assert table is not None
    return MonthlyTableRefreshResponseDTO(
        table=table, requests_used=requests_used, months_collected=to_collect
    )


@router.post("/refresh", responses={404: {"model": ErrorDTO}, 409: {"model": ErrorDTO}})
async def refresh_monthly_table(
    client_id: str, force: str | None = Query(default=None)
) -> MonthlyTableRefreshResponseDTO:
    try:
        return _refresh(client_id, force=force)
    except NotConfiguredError as exc:
        if str(exc) == "client_not_found":
            raise HTTPException(404, _CLIENT_NOT_FOUND) from exc
        raise HTTPException(409, _NOT_CONFIGURED) from exc


@router.get("/key-events", responses={404: {"model": ErrorDTO}, 409: {"model": ErrorDTO}})
async def get_key_event_candidates(client_id: str) -> KeyEventCandidatesDTO:
    api_key = os.environ.get("REPORTING_NINJA_API_KEY", "")
    with worker_session_scope() as session:
        client = ClientRepository(session).get(client_id)
        if client is None:
            raise HTTPException(404, _CLIENT_NOT_FOUND)
        refs = _ninja_refs(session, client_id)
        if refs is None or not api_key:
            raise HTTPException(409, _NOT_CONFIGURED)
        connection_key, ga4_account_id, _report_name, _gsc_ref = refs
        repo = ClientMonthlyRepository(session)
        settings_row = repo.get_settings(client_id)
        attribution_events: monthly_table.Attribution = (
            settings_row.attribution_events  # type: ignore[assignment]
            if settings_row
            else _DEFAULT_ATTRIBUTION_EVENTS
        )

    with ReportingNinjaClient(api_key) as client_rn:
        ga4_fields = monthly_table.discover_ga4_monthly_fields(client_rn)
        candidates = monthly_table.discover_key_events(
            client_rn,
            ga4_connection_key=connection_key,
            ga4_account_id=ga4_account_id,
            ga4_fields=ga4_fields,
            attribution_events=attribution_events,
        )
    return KeyEventCandidatesDTO(
        candidates=[
            KeyEventCandidateDTO(
                event_name=c["event_name"], key_events=c["key_events"], months_seen=c["months_seen"]
            )
            for c in candidates
        ]
    )


@router.put(
    "/settings",
    responses={
        404: {"model": ErrorDTO},
        409: {
            "model": ErrorDTO,
            "description": "attribution_changed (non-blocking — settings were saved)",
        },
    },
)
async def put_monthly_table_settings(
    client_id: str, body: MonthlyTableSettingsUpdateRequest
) -> MonthlyTableSettingsDTO:
    with worker_session_scope() as session:
        client = ClientRepository(session).get(client_id)
        if client is None:
            raise HTTPException(404, _CLIENT_NOT_FOUND)
        repo = ClientMonthlyRepository(session)

        stale_months: list[str] = []
        if body.attribution_users is not None or body.attribution_events is not None:
            current = repo.get_or_create_settings(client_id)
            next_users = body.attribution_users or current.attribution_users
            next_events = body.attribution_events or current.attribution_events
            stale_months = repo.months_with_stale_attribution(
                client_id, attribution_users=next_users, attribution_events=next_events
            )

        updated = repo.update_settings(
            client_id,
            key_event_names_json=body.key_event_names,
            key_event_label=body.key_event_label,
            attribution_users=body.attribution_users,
            attribution_events=body.attribution_events,
            history_months=body.history_months,
        )
        repo.commit()
        dto = _settings_dto(updated)

    if stale_months:
        raise HTTPException(
            409,
            ErrorDTO(
                error_code="attribution_changed",
                message=(
                    f"Settings saved. {len(stale_months)} already-collected month(s) were "
                    "gathered under the previous attribution — force-refresh them to match."
                ),
                details=AttributionChangedDetailDTO(affected_months=stale_months).model_dump(),
            ).model_dump(),
        )
    return dto


def _build_xlsx(client_id: str) -> tuple[str, bytes] | None:
    """`(filename, bytes)` or `None` for an unknown client. Reuses the exact
    same stored rows the JSON table reads — the export is never a second,
    independently-derived copy of the numbers (task-54 §7: "точ в точ").
    """
    with worker_session_scope() as session:
        client = ClientRepository(session).get(client_id)
        if client is None:
            return None
        repo = ClientMonthlyRepository(session)
        settings_row = repo.get_settings(client_id)
        app_settings = get_settings()
        months = _effective_months(
            settings_row, default_history_months=app_settings.client_monthly_history_months
        )
        keys = month_keys(count=months)
        stored = repo.list_window(client_id, keys)
        rows = _built_monthly_rows(keys, stored)
        has_key_event_column = bool(settings_row and settings_row.key_event_names_json)
        key_event_label = settings_row.key_event_label if settings_row else None
        client_name = client.name

    data = monthly_xlsx.build_monthly_xlsx(
        rows, key_event_label=key_event_label, has_key_event_column=has_key_event_column
    )
    safe_name = "".join(c if c.isalnum() else "_" for c in client_name).strip("_") or client_id
    filename = f"{safe_name}_Monthly_Performance.xlsx"
    return filename, data


@router.get("/xlsx", responses={404: {"model": ErrorDTO}})
async def export_monthly_table_xlsx(client_id: str) -> StreamingResponse:
    result = _build_xlsx(client_id)
    if result is None:
        raise HTTPException(404, _CLIENT_NOT_FOUND)
    filename, data = result
    return StreamingResponse(
        iter([data]),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


register_router(router)
