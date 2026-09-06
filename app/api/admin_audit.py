"""Читання журналу дій (task-72). Тільки для адміна.

Запису тут немає навмисно: журнал наповнює лише `AuditSink`, і жодного
шляху «дописати рядок ззовні» не існує — інакше сам журнал перестав би бути
свідченням.
"""

from __future__ import annotations

import csv
import io
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.api import register_router
from app.api.deps import AdminActorDep, SessionDep, SettingsDep
from app.db.models import AuditEvent
from app.db.repositories.audit import AuditRepository
from app.schemas.audit import AuditEventDTO, AuditFacetsDTO, AuditPageDTO

router = APIRouter(prefix="/api/v1/admin/audit", tags=["admin"])

_CURSOR_SEPARATOR = "|"


def _to_dto(event: AuditEvent) -> AuditEventDTO:
    return AuditEventDTO(
        id=event.id,
        ts=event.ts,
        actor_kind=event.actor_kind,
        actor_id=event.actor_id,
        actor_email=event.actor_email,
        actor_name=event.actor_name,
        actor_role=event.actor_role,
        ip=event.ip,
        user_agent=event.user_agent,
        action=event.action,
        resource_type=event.resource_type,
        resource_id=event.resource_id,
        resource_label=event.resource_label,
        method=event.method,
        path=event.path,
        status_code=event.status_code,
        outcome=event.outcome,
        duration_ms=event.duration_ms,
        details=event.details_json or {},
    )


def _parse_cursor(raw: str | None) -> tuple[datetime, str] | None:
    if not raw:
        return None
    try:
        ts_text, event_id = raw.split(_CURSOR_SEPARATOR, 1)
        return datetime.fromisoformat(ts_text), event_id
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail={"error_code": "bad_cursor", "message": "Malformed pagination cursor"},
        ) from None


def _filters(
    actor_id: str | None,
    actor_kind: str | None,
    action: str | None,
    resource_type: str | None,
    resource_id: str | None,
    outcome: str | None,
    since: datetime | None,
    until: datetime | None,
    q: str | None,
) -> dict[str, Any]:
    return {
        "actor_id": actor_id,
        "actor_kind": actor_kind,
        "action": action,
        "resource_type": resource_type,
        "resource_id": resource_id,
        "outcome": outcome,
        "since": since,
        "until": until,
        "search": q,
    }


@router.get("")
async def list_audit(
    session: SessionDep,
    actor: AdminActorDep,
    actor_id: Annotated[str | None, Query()] = None,
    actor_kind: Annotated[str | None, Query()] = None,
    action: Annotated[str | None, Query()] = None,
    resource_type: Annotated[str | None, Query()] = None,
    resource_id: Annotated[str | None, Query()] = None,
    outcome: Annotated[str | None, Query()] = None,
    since: Annotated[datetime | None, Query()] = None,
    until: Annotated[datetime | None, Query()] = None,
    q: Annotated[str | None, Query(max_length=200)] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    cursor: Annotated[str | None, Query()] = None,
) -> AuditPageDTO:
    parsed_cursor = _parse_cursor(cursor)
    filters = _filters(
        actor_id, actor_kind, action, resource_type, resource_id, outcome, since, until, q
    )

    def _query(db: Session) -> AuditPageDTO:
        events = AuditRepository(db).query(limit=limit + 1, cursor=parsed_cursor, **filters)
        has_more = len(events) > limit
        page = events[:limit]
        next_cursor = None
        if has_more and page:
            last = page[-1]
            next_cursor = f"{last.ts.isoformat()}{_CURSOR_SEPARATOR}{last.id}"
        return AuditPageDTO(events=[_to_dto(event) for event in page], next_cursor=next_cursor)

    return await session.run_sync(_query)


@router.get("/facets")
async def audit_facets(session: SessionDep, actor: AdminActorDep) -> AuditFacetsDTO:
    def _facets(db: Session) -> AuditFacetsDTO:
        return AuditFacetsDTO(**AuditRepository(db).facets())

    return await session.run_sync(_facets)


def _csv_safe(value: Any) -> str:
    """Знешкоджує formula injection у Excel/Sheets.

    Клітинка, що починається з `=`, `+`, `-` чи `@`, виконується як формула
    при відкритті файлу — а вміст журналу приходить із зовнішнього світу
    (user-agent, шляхи, тіла запитів).
    """
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in {"=", "+", "-", "@"} else text


@router.get("/export.csv")
async def export_audit(
    session: SessionDep,
    settings: SettingsDep,
    actor: AdminActorDep,
    actor_id: Annotated[str | None, Query()] = None,
    actor_kind: Annotated[str | None, Query()] = None,
    action: Annotated[str | None, Query()] = None,
    resource_type: Annotated[str | None, Query()] = None,
    resource_id: Annotated[str | None, Query()] = None,
    outcome: Annotated[str | None, Query()] = None,
    since: Annotated[datetime | None, Query()] = None,
    until: Annotated[datetime | None, Query()] = None,
    q: Annotated[str | None, Query(max_length=200)] = None,
) -> StreamingResponse:
    filters = _filters(
        actor_id, actor_kind, action, resource_type, resource_id, outcome, since, until, q
    )

    def _rows(db: Session) -> list[list[str]]:
        events = AuditRepository(db).query(limit=settings.audit_export_max_rows, **filters)
        return [
            [
                _csv_safe(event.ts.isoformat()),
                _csv_safe(event.actor_email or event.actor_kind),
                _csv_safe(event.actor_role),
                _csv_safe(event.action),
                _csv_safe(event.outcome),
                _csv_safe(event.resource_type),
                _csv_safe(event.resource_id),
                _csv_safe(event.method),
                _csv_safe(event.path),
                _csv_safe(event.status_code),
                _csv_safe(event.ip),
            ]
            for event in events
        ]

    rows = await session.run_sync(_rows)
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        ["when", "who", "role", "action", "outcome", "resource", "resource_id",
         "method", "path", "status", "ip"]
    )
    writer.writerows(rows)
    buffer.seek(0)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M")
    return StreamingResponse(
        iter([buffer.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="audit-{stamp}.csv"'},
    )


@router.get("/{event_id}")
async def get_audit_event(
    event_id: str, session: SessionDep, actor: AdminActorDep
) -> AuditEventDTO:
    def _get(db: Session) -> AuditEventDTO:
        event = AuditRepository(db).get(event_id)
        if event is None:
            raise HTTPException(
                status_code=404,
                detail={"error_code": "event_not_found", "message": "Audit event not found"},
            )
        return _to_dto(event)

    return await session.run_sync(_get)


register_router(router)
