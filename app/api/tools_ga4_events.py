"""GA4 Events by Month — ендпоінти тула (task-98 §2.4).

Свідомо **не** через job-рушій, як і решта тулів: прогін — це N дешевих
запитів до GA4 Data API (один на місяць, заміряно 0.22-0.82 с кожен), а не
колекція метрик за період. Блокуючий мережевий шлях іде через
`run_in_threadpool`, як у `tools_schema_markup.py`/`tools_alt_text.py`.

Мутуючий ендпоінт тут один (`POST /reports`) і комітить сам — `SessionDep`
віддає сесію без auto-commit.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.api import register_router
from app.api.deps import AttributionDep, PaginationDep, SessionDep
from app.db.models import Client, Ga4EventMonthReport
from app.db.repositories.clients import ClientRepository
from app.db.repositories.ga4_event_reports import (
    Ga4EventReportInput,
    Ga4EventReportRepository,
)
from app.schemas.api import ErrorDTO
from app.schemas.ga4_events import (
    Ga4EventMonthDTO,
    Ga4EventRowDTO,
    Ga4EventsClientDTO,
    Ga4EventsClientsDTO,
    Ga4EventSegmentDTO,
    Ga4EventsMonthsDTO,
    Ga4EventsReportDTO,
    Ga4EventsReportRequest,
    Ga4EventsReportsPageDTO,
    Ga4EventsReportSummaryDTO,
    Ga4EventTotalsDTO,
)
from app.tools.ga4_events.service import (
    Ga4EventsConfigError,
    MonthResult,
    ReportResult,
    available_months,
    normalize_months,
    resolve_property_id,
    run_events_report,
)
from app.tools.ga4_events.xlsx import build_workbook

router = APIRouter(prefix="/api/v1/tools/ga4-events", tags=["ga4-events"])


def _error(code: str, message: str) -> dict[str, object]:
    return ErrorDTO(error_code=code, message=message).model_dump()


def _not_configured(detail: str) -> HTTPException:
    """422, не 500 і не 404: клієнт існує, запит валідний — просто питати нема
    кого. Фронт показує це як стан `not_configured`, а не як збій."""
    return HTTPException(status_code=422, detail=_error("ga4_not_configured", detail))


def _month_dto(month: MonthResult) -> Ga4EventMonthDTO:
    return Ga4EventMonthDTO(
        month=month.month,
        label=month.label,
        state=month.state,  # type: ignore[arg-type]
        is_partial=month.is_partial,
        rows=[
            Ga4EventRowDTO(
                event_name=row.event_name,
                is_key_event=row.is_key_event,
                key_events=row.key_events,
                segments={
                    key: Ga4EventSegmentDTO(**asdict(values))
                    for key, values in row.segments.items()
                },
            )
            for row in month.rows
        ],
        totals={
            key: Ga4EventTotalsDTO(**asdict(values)) for key, values in month.totals.items()
        },
        error=month.error,
    )


def _months_payload(result: ReportResult) -> list[dict[str, Any]]:
    """Те, що лягає в `rows_json` — той самий DTO, що поїде у відповідь.
    Одна форма на відповідь, знімок і Excel: три незалежні похідні від однієї
    відповіді GA4 неминуче розійшлись би."""
    return [_month_dto(month).model_dump(mode="json") for month in result.months]


def _summary_dto(
    report: Ga4EventMonthReport, *, client_name: str | None
) -> Ga4EventsReportSummaryDTO:
    return Ga4EventsReportSummaryDTO(
        id=report.id,
        client_id=report.client_id,
        client_name=client_name,
        property_id=report.property_id,
        months=list(report.months_json or []),
        state=report.state,  # type: ignore[arg-type]
        created_at=report.created_at,
    )


def _detail_dto(
    report: Ga4EventMonthReport, *, client_name: str | None
) -> Ga4EventsReportDTO:
    summary = _summary_dto(report, client_name=client_name)
    return Ga4EventsReportDTO(
        **summary.model_dump(),
        months_data=[Ga4EventMonthDTO(**month) for month in (report.rows_json or [])],
    )


def _client_name(sync_session: Session, client_id: str | None) -> str | None:
    if not client_id:
        return None
    client = ClientRepository(sync_session).get(client_id)
    return client.name if client else None


@router.get("/clients", response_model=Ga4EventsClientsDTO)
async def list_clients(session: SessionDep) -> Ga4EventsClientsDTO:
    """Клієнти + чи є в них GA4-property. Неналаштовані НЕ ховаються: зникнути
    зі списку — гірше за чесне «GA4 isn't connected», бо виглядає як «клієнта
    не існує»."""

    def _read(sync_session: Session) -> Ga4EventsClientsDTO:
        rows: list[Ga4EventsClientDTO] = []
        clients: list[Client] = ClientRepository(sync_session).list_active()
        for client in clients:
            try:
                property_id: str | None = resolve_property_id(
                    sync_session, client_id=client.id
                )
            except Ga4EventsConfigError:
                property_id = None
            rows.append(
                Ga4EventsClientDTO(
                    id=client.id,
                    name=client.name,
                    configured=property_id is not None,
                    property_id=property_id,
                )
            )
        rows.sort(key=lambda row: ((not row.configured), row.name.lower()))
        return Ga4EventsClientsDTO(clients=rows)

    return await session.run_sync(_read)


@router.get(
    "/clients/{client_id}/months",
    response_model=Ga4EventsMonthsDTO,
    responses={404: {"model": ErrorDTO}, 422: {"model": ErrorDTO}},
)
async def list_months(client_id: str, session: SessionDep) -> Ga4EventsMonthsDTO:
    """Місяці, у яких у property реально є події (один запит, 1.0-1.3 с).

    Форма вибору будується з цього списку, тож оператор не може обрати місяць,
    якого не існує, і не отримує порожню вкладку як «результат».
    """

    def _exists(sync_session: Session) -> bool:
        return ClientRepository(sync_session).get(client_id) is not None

    if not await session.run_sync(_exists):
        raise HTTPException(
            status_code=404, detail=_error("client_not_found", "Client not found")
        )

    def _property(sync_session: Session) -> str:
        return resolve_property_id(sync_session, client_id=client_id)

    try:
        property_id = await session.run_sync(_property)
        months = await run_in_threadpool(available_months, property_id=property_id)
    except Ga4EventsConfigError as exc:
        raise _not_configured(exc.detail) from None
    return Ga4EventsMonthsDTO(client_id=client_id, months=months)


@router.post(
    "/reports",
    response_model=Ga4EventsReportDTO,
    responses={404: {"model": ErrorDTO}, 422: {"model": ErrorDTO}},
)
async def create_report(
    payload: Ga4EventsReportRequest, session: SessionDep, created_by: AttributionDep
) -> Ga4EventsReportDTO:
    try:
        months = normalize_months(payload.months)
    except ValueError as exc:
        raise HTTPException(
            status_code=422, detail=_error("invalid_months", str(exc))
        ) from None

    def _load(sync_session: Session) -> tuple[str | None, str | None]:
        """Ім'я клієнта і його property — одним коротким читанням БД, ДО
        походу в Google: далі мережевий шлях працює без жодної сесії."""
        name = _client_name(sync_session, payload.client_id)
        if name is None:
            return None, None
        try:
            return name, resolve_property_id(sync_session, client_id=payload.client_id)
        except Ga4EventsConfigError as exc:
            raise _not_configured(exc.detail) from None

    client_name, property_id = await session.run_sync(_load)
    if client_name is None or property_id is None:
        raise HTTPException(
            status_code=404, detail=_error("client_not_found", "Client not found")
        )

    try:
        result: ReportResult = await run_in_threadpool(
            run_events_report, property_id=property_id, months=months
        )
    except Ga4EventsConfigError as exc:
        raise _not_configured(exc.detail) from None

    payload_months = _months_payload(result)

    def _persist(sync_session: Session) -> Ga4EventsReportDTO:
        report = Ga4EventReportRepository(sync_session).create_report(
            Ga4EventReportInput(
                client_id=payload.client_id,
                property_id=result.property_id,
                months=[month.month for month in result.months],
                state=result.state,
                rows=payload_months,
                created_by_user_id=created_by.user_id,
                created_by_kind=created_by.kind,
            )
        )
        return _detail_dto(report, client_name=client_name)

    dto = await session.run_sync(_persist)
    await session.commit()
    return dto


@router.get("/reports", response_model=Ga4EventsReportsPageDTO)
async def list_reports(
    session: SessionDep, pagination: PaginationDep, client_id: str | None = None
) -> Ga4EventsReportsPageDTO:
    def _read(sync_session: Session) -> Ga4EventsReportsPageDTO:
        reports, total = Ga4EventReportRepository(sync_session).list_reports(
            client_id=client_id, limit=pagination.limit, offset=pagination.offset
        )
        return Ga4EventsReportsPageDTO(
            total=total,
            limit=pagination.limit,
            offset=pagination.offset,
            reports=[
                _summary_dto(r, client_name=_client_name(sync_session, r.client_id))
                for r in reports
            ],
        )

    return await session.run_sync(_read)


@router.get(
    "/reports/{report_id}",
    response_model=Ga4EventsReportDTO,
    responses={404: {"model": ErrorDTO}},
)
async def get_report(report_id: str, session: SessionDep) -> Ga4EventsReportDTO:
    def _read(sync_session: Session) -> Ga4EventsReportDTO | None:
        report = Ga4EventReportRepository(sync_session).get_report(report_id)
        if report is None:
            return None
        return _detail_dto(report, client_name=_client_name(sync_session, report.client_id))

    dto = await session.run_sync(_read)
    if dto is None:
        raise HTTPException(
            status_code=404, detail=_error("report_not_found", "Report not found")
        )
    return dto


@router.get(
    "/reports/{report_id}/export.xlsx",
    responses={404: {"model": ErrorDTO}},
    response_class=StreamingResponse,
)
async def export_report(report_id: str, session: SessionDep) -> StreamingResponse:
    """Excel із ТОГО САМОГО знімка, що й на екрані — ніколи не другий,
    незалежно здобутий набір чисел (правило `pageinventory/xlsx.py`)."""

    def _read(sync_session: Session) -> Ga4EventsReportDTO | None:
        report = Ga4EventReportRepository(sync_session).get_report(report_id)
        if report is None:
            return None
        return _detail_dto(report, client_name=_client_name(sync_session, report.client_id))

    dto = await session.run_sync(_read)
    if dto is None:
        raise HTTPException(
            status_code=404, detail=_error("report_not_found", "Report not found")
        )
    stream = build_workbook(dto)
    stamp = dto.created_at.strftime("%Y%m%d") if isinstance(dto.created_at, datetime) else ""
    slug = (dto.client_name or "client").lower().replace(" ", "-")
    filename = f"ga4-events-{slug}-{stamp}.xlsx"
    return StreamingResponse(
        stream,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


register_router(router)
