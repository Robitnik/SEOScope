"""Indexing Planner — ендпоінти тула (task-100 §2.4).

Свідомо не через job-рушій, як і решта тулів: скан — це N дешевих запитів
до Google API, а не колекція метрик за період. Блокуючий мережевий шлях
іде через `run_in_threadpool`.

Ролі — загальні: мутація вимагає `member`+, читання відкрите `viewer`.
Окремого запису в `DEFAULT_ADMIN_WRITE_ONLY_PATHS` тут немає навмисно:
дорого коштує не план, а ручна робота людини, і відмітити відправленим
має вміти той, хто цю роботу робить.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.api import register_router
from app.api.deps import AttributionDep, SessionDep
from app.core.config import get_settings
from app.db.base import utcnow
from app.db.models import IndexingSubmission, User
from app.db.repositories.indexing import IndexingRepository
from app.indexing.analytics import build_analytics
from app.indexing.context import load_client_context
from app.indexing.dto import (
    client_dto,
    scan_dto,
    settings_dto,
    submission_dto,
    titles_for,
    url_dto,
)
from app.indexing.plan import forecast
from app.indexing.service import run_portfolio_scan, site_url_for
from app.schemas.api import ErrorDTO
from app.schemas.indexing import (
    IndexingAnalyticsDTO,
    IndexingCalendarCellDTO,
    IndexingCalendarDTO,
    IndexingClientsDTO,
    IndexingDayGroupDTO,
    IndexingOverviewDTO,
    IndexingQueueDTO,
    IndexingScanDTO,
    IndexingScanRequest,
    IndexingSettingsDTO,
    IndexingSettingsUpdate,
    IndexingSubmissionDTO,
    IndexingTodayDTO,
    IndexingUrlDTO,
)

router = APIRouter(prefix="/api/v1/tools/indexing", tags=["indexing"])


def _error(code: str, message: str) -> dict[str, object]:
    return ErrorDTO(error_code=code, message=message).model_dump()


def _user_name(sync_session: Session, user_id: str | None) -> str | None:
    if not user_id:
        return None
    user = sync_session.get(User, user_id)
    return user.name if user is not None else None


# --- клієнти й зведення ---------------------------------------------------


@router.get("/clients", response_model=IndexingClientsDTO)
async def list_clients(session: SessionDep) -> IndexingClientsDTO:
    """Усі активні клієнти. Неналаштовані НЕ ховаються: зникнути зі списку
    гірше за чесне «Search Console isn't connected»."""

    def _read(sync_session: Session) -> IndexingClientsDTO:
        repo = IndexingRepository(sync_session)
        scans = repo.last_scan_per_client()
        rows = []
        for client in repo.active_clients():
            settings = repo.get_or_create_settings(client.id)
            counts = repo.client_counts(client.id)
            rows.append(
                client_dto(
                    client,
                    settings,
                    site_url=site_url_for(sync_session, client.id),
                    counts=counts if counts["urls_total"] else None,
                    last_scan=scans.get(client.id),
                )
            )
        sync_session.commit()
        rows.sort(key=lambda row: ((not row.configured), row.name.lower()))
        return IndexingClientsDTO(clients=rows)

    return await session.run_sync(_read)


@router.get("/overview", response_model=IndexingOverviewDTO)
async def overview(session: SessionDep) -> IndexingOverviewDTO:
    def _read(sync_session: Session) -> IndexingOverviewDTO:
        repo = IndexingRepository(sync_session)
        clients = repo.active_clients()
        settings_map = repo.settings_map()
        configured = [c for c in clients if site_url_for(sync_session, c.id)]
        enabled_ids = [
            c.id
            for c in configured
            if (s := settings_map.get(c.id)) is not None and s.enabled
        ]
        counts = repo.portfolio_counts([c.id for c in configured])
        has_data = counts["urls_total"] > 0
        capacity = sum(
            settings_map[cid].daily_quota for cid in enabled_ids if cid in settings_map
        )
        today = utcnow().date()
        submissions = repo.submissions_for_day(today)
        days, holds = forecast(
            queue_size=counts["candidates"],
            daily_capacity=capacity,
            cycle_days=min(
                (settings_map[cid].resubmit_after_days for cid in enabled_ids),
                default=90,
            ),
        )
        last = repo.last_scan()
        return IndexingOverviewDTO(
            clients_enabled=len(enabled_ids),
            clients_configured=len(configured),
            urls_total=counts["urls_total"] if has_data else None,
            indexed=counts["indexed"] if has_data else None,
            candidates=counts["candidates"] if has_data else None,
            excluded=counts["excluded"] if has_data else None,
            never_inspected=counts["never_inspected"] if has_data else None,
            coverage_ratio=(
                (counts["indexed"] / counts["urls_total"]) if has_data else None  # type: ignore[arg-type]
            ),
            daily_capacity=capacity,
            planned_today=sum(1 for row in submissions if row.status == "planned"),
            sent_today=sum(1 for row in submissions if row.status == "sent"),
            last_scan_at=last.started_at if last else None,
            last_scan_status=last.status if last else None,  # type: ignore[arg-type]
            days_to_clear_queue=days,
            cycle_holds=holds,
        )

    return await session.run_sync(_read)


@router.put(
    "/clients/{client_id}/settings",
    response_model=IndexingSettingsDTO,
    responses={404: {"model": ErrorDTO}},
)
async def update_settings(
    client_id: str, payload: IndexingSettingsUpdate, session: SessionDep
) -> IndexingSettingsDTO:
    def _write(sync_session: Session) -> IndexingSettingsDTO:
        repo = IndexingRepository(sync_session)
        names = repo.client_names()
        if client_id not in names:
            raise HTTPException(404, _error("client_not_found", "No such client."))
        row = repo.get_or_create_settings(client_id)
        for field, value in payload.model_dump(exclude_none=True).items():
            setattr(row, field, value)
        repo.commit()
        return settings_dto(row)

    return await session.run_sync(_write)


# --- сьогодні й календар --------------------------------------------------


def _day_groups(
    sync_session: Session, day: date, *, only_client: str | None = None
) -> list[IndexingDayGroupDTO]:
    repo = IndexingRepository(sync_session)
    names = repo.client_names()
    settings_map = repo.settings_map()
    client_ids = [only_client] if only_client else None
    submissions = repo.submissions_for_day(day, client_ids=client_ids)

    by_client: dict[str, list[IndexingSubmission]] = {}
    for row in submissions:
        by_client.setdefault(row.client_id, []).append(row)

    groups: list[IndexingDayGroupDTO] = []
    for client_id, rows in by_client.items():
        settings = settings_map.get(client_id)
        url_map = {row.indexing_url_id: repo.get_url(row.indexing_url_id) for row in rows}
        keys = {url.url_key for url in url_map.values() if url is not None}
        titles = titles_for(sync_session, client_id=client_id, url_keys=keys)
        items = [
            submission_dto(
                row,
                url_map.get(row.indexing_url_id),
                client_name=names.get(client_id, client_id),
                submitted_by=_user_name(sync_session, row.submitted_by_user_id),
                title=(
                    titles.get(url.url_key)
                    if (url := url_map.get(row.indexing_url_id)) is not None
                    else None
                ),
            )
            for row in rows
        ]
        quota = settings.daily_quota if settings else 0
        groups.append(
            IndexingDayGroupDTO(
                client_id=client_id,
                client_name=names.get(client_id, client_id),
                daily_quota=quota,
                planned=sum(1 for row in rows if row.status == "planned"),
                sent=sum(1 for row in rows if row.status == "sent"),
                quota_blocked=sum(1 for row in rows if row.status == "quota_blocked"),
                submissions=items,
                # Порожній день — нормальний стан (сім із десяти клієнтів
                # мають беклог <= 3 URL), тому це факт, а не помилка.
                queue_exhausted=len(rows) < quota and repo.queue_size(client_id) == 0,
            )
        )
    groups.sort(key=lambda group: group.client_name.lower())
    return groups


@router.get("/today", response_model=IndexingTodayDTO)
async def today_plan(session: SessionDep) -> IndexingTodayDTO:
    def _read(sync_session: Session) -> IndexingTodayDTO:
        day = utcnow().date()
        groups = _day_groups(sync_session, day)
        return IndexingTodayDTO(
            day=day,
            groups=groups,
            planned_total=sum(group.planned for group in groups),
            sent_total=sum(group.sent for group in groups),
        )

    return await session.run_sync(_read)


@router.get("/calendar", response_model=IndexingCalendarDTO)
async def calendar(
    session: SessionDep, days: int = Query(default=8, ge=1, le=31)
) -> IndexingCalendarDTO:
    def _read(sync_session: Session) -> IndexingCalendarDTO:
        repo = IndexingRepository(sync_session)
        settings_map = repo.settings_map()
        scans = repo.last_scan_per_client()
        today = utcnow().date()
        span = [today + timedelta(days=offset) for offset in range(days)]
        rows = repo.submissions_in_range(span[0], span[-1])

        cells: dict[tuple[str, date], IndexingCalendarCellDTO] = {}
        for row in rows:
            key = (row.client_id, row.planned_for)
            cell = cells.get(key)
            if cell is None:
                cell = IndexingCalendarCellDTO(
                    client_id=row.client_id, day=row.planned_for, planned=0, sent=0
                )
                cells[key] = cell
            if row.status == "sent":
                cell.sent += 1
            elif row.status == "planned":
                cell.planned += 1
            cell.reason_counts[row.reason_code] = cell.reason_counts.get(row.reason_code, 0) + 1

        clients = []
        queue_size = 0
        capacity = 0
        for client in repo.active_clients():
            settings = repo.get_or_create_settings(client.id)
            counts = repo.client_counts(client.id)
            clients.append(
                client_dto(
                    client,
                    settings,
                    site_url=site_url_for(sync_session, client.id),
                    counts=counts if counts["urls_total"] else None,
                    last_scan=scans.get(client.id),
                )
            )
            if settings.enabled:
                queue_size += counts["candidates"]
                capacity += settings.daily_quota
        sync_session.commit()

        cycle = min(
            (s.resubmit_after_days for s in settings_map.values() if s.enabled), default=90
        )
        to_clear, holds = forecast(
            queue_size=queue_size, daily_capacity=capacity, cycle_days=cycle
        )
        return IndexingCalendarDTO(
            days=span,
            frozen_day=today,
            clients=clients,
            cells=list(cells.values()),
            queue_size=queue_size,
            daily_capacity=capacity,
            days_to_clear_queue=to_clear,
            cycle_holds=holds,
        )

    return await session.run_sync(_read)


# --- черга ----------------------------------------------------------------


@router.get("/queue", response_model=IndexingQueueDTO)
async def queue(
    session: SessionDep,
    client_id: str | None = None,
    reason_code: str | None = None,
    excluded: bool = False,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
) -> IndexingQueueDTO:
    def _read(sync_session: Session) -> IndexingQueueDTO:
        repo = IndexingRepository(sync_session)
        names = repo.client_names()
        settings_map = repo.settings_map()
        rows, total = repo.queue(
            client_ids=[client_id] if client_id else None,
            reason_code=reason_code,
            excluded=excluded,
            offset=offset,
            limit=limit,
        )
        contexts: dict[str, object] = {}
        items: list[IndexingUrlDTO] = []
        for row in rows:
            if row.client_id not in contexts:
                contexts[row.client_id] = load_client_context(
                    sync_session, client_id=row.client_id
                )
            settings = settings_map.get(row.client_id)
            items.append(
                url_dto(
                    row,
                    client_name=names.get(row.client_id, row.client_id),
                    context=contexts[row.client_id],  # type: ignore[arg-type]
                    resubmit_after_days=settings.resubmit_after_days if settings else 90,
                )
            )
        return IndexingQueueDTO(urls=items, total=total, offset=offset, limit=limit)

    return await session.run_sync(_read)


@router.get(
    "/urls/{url_id}",
    response_model=dict,
    responses={404: {"model": ErrorDTO}},
)
async def url_detail(url_id: str, session: SessionDep) -> dict[str, Any]:
    def _read(sync_session: Session) -> dict[str, Any]:
        repo = IndexingRepository(sync_session)
        row = repo.get_url(url_id)
        if row is None:
            raise HTTPException(404, _error("url_not_found", "No such URL."))
        names = repo.client_names()
        settings = repo.get_or_create_settings(row.client_id)
        context = load_client_context(sync_session, client_id=row.client_id)
        history = [
            submission_dto(
                item,
                row,
                client_name=names.get(row.client_id, row.client_id),
                submitted_by=_user_name(sync_session, item.submitted_by_user_id),
            )
            for item in repo.submissions_for_url(url_id)
        ]
        sync_session.commit()
        return {
            "url": url_dto(
                row,
                client_name=names.get(row.client_id, row.client_id),
                context=context,
                resubmit_after_days=settings.resubmit_after_days,
            ).model_dump(mode="json"),
            "history": [item.model_dump(mode="json") for item in history],
        }

    return await session.run_sync(_read)


@router.post(
    "/urls/{url_id}/exclude",
    response_model=IndexingUrlDTO,
    responses={404: {"model": ErrorDTO}},
)
async def exclude_url(
    url_id: str, session: SessionDep, attribution: AttributionDep
) -> IndexingUrlDTO:
    return await _set_exclusion(session, url_id, excluded=True, attribution=attribution)


@router.post(
    "/urls/{url_id}/include",
    response_model=IndexingUrlDTO,
    responses={404: {"model": ErrorDTO}},
)
async def include_url(
    url_id: str, session: SessionDep, attribution: AttributionDep
) -> IndexingUrlDTO:
    return await _set_exclusion(session, url_id, excluded=False, attribution=attribution)


async def _set_exclusion(
    session: SessionDep, url_id: str, *, excluded: bool, attribution: AttributionDep
) -> IndexingUrlDTO:
    def _write(sync_session: Session) -> IndexingUrlDTO:
        repo = IndexingRepository(sync_session)
        row = repo.get_url(url_id)
        if row is None:
            raise HTTPException(404, _error("url_not_found", "No such URL."))
        row.excluded = excluded
        # `manual` переживає перескорювання — автоматика його не знімає.
        row.excluded_reason = "manual" if excluded else None
        row.excluded_by_user_id = attribution.user_id if excluded else None
        if excluded:
            row.reason_code = None
            row.reason_json = {}
            row.priority_score = None
        repo.commit()
        names = repo.client_names()
        return url_dto(row, client_name=names.get(row.client_id, row.client_id))

    return await session.run_sync(_write)


# --- відмітки -------------------------------------------------------------


async def _transition(
    session: SessionDep,
    submission_id: str,
    *,
    kind: str,
    user_id: str | None = None,
) -> IndexingSubmissionDTO:
    def _write(sync_session: Session) -> IndexingSubmissionDTO:
        repo = IndexingRepository(sync_session)
        row = repo.get_submission(submission_id)
        if row is None:
            raise HTTPException(404, _error("submission_not_found", "No such submission."))
        if kind == "sent":
            repo.mark_sent(row, user_id=user_id)
        elif kind == "quota_blocked":
            repo.mark_quota_blocked(row)
        elif kind == "undo" and not repo.undo_submission(row):
            raise HTTPException(
                409,
                _error(
                    "undo_window_closed",
                    "This submission can no longer be undone (24-hour window).",
                ),
            )
        repo.commit()
        names = repo.client_names()
        url = repo.get_url(row.indexing_url_id)
        return submission_dto(
            row,
            url,
            client_name=names.get(row.client_id, row.client_id),
            submitted_by=_user_name(sync_session, row.submitted_by_user_id),
        )

    return await session.run_sync(_write)


@router.post(
    "/submissions/{submission_id}/sent",
    response_model=IndexingSubmissionDTO,
    responses={404: {"model": ErrorDTO}},
)
async def mark_sent(
    submission_id: str, session: SessionDep, attribution: AttributionDep
) -> IndexingSubmissionDTO:
    """Ідемпотентно: повторний виклик не створює другої відправки й не
    зсуває cooldown."""
    return await _transition(
        session, submission_id, kind="sent", user_id=attribution.user_id
    )


@router.post(
    "/submissions/{submission_id}/quota-blocked",
    response_model=IndexingSubmissionDTO,
    responses={404: {"model": ErrorDTO}},
)
async def mark_quota_blocked(
    submission_id: str, session: SessionDep
) -> IndexingSubmissionDTO:
    """GSC відмовив по квоті — це НЕ відправка: cooldown не ставиться, і
    завтрашній переплан візьме цей URL знову."""
    return await _transition(session, submission_id, kind="quota_blocked")


@router.post(
    "/submissions/{submission_id}/undo",
    response_model=IndexingSubmissionDTO,
    responses={404: {"model": ErrorDTO}, 409: {"model": ErrorDTO}},
)
async def undo_submission(submission_id: str, session: SessionDep) -> IndexingSubmissionDTO:
    return await _transition(session, submission_id, kind="undo")


# --- скани ----------------------------------------------------------------


@router.post("/scans", response_model=IndexingScanDTO, responses={409: {"model": ErrorDTO}})
async def start_scan(
    payload: IndexingScanRequest, session: SessionDep, attribution: AttributionDep
) -> IndexingScanDTO:
    settings = get_settings()

    def _guard(sync_session: Session) -> None:
        repo = IndexingRepository(sync_session)
        from app.indexing.service import SCAN_STALE_AFTER

        if payload.client_id is None and repo.running_portfolio_scan(
            stale_after=SCAN_STALE_AFTER
        ):
            raise HTTPException(
                409,
                _error("scan_already_running", "A portfolio scan is already running."),
            )

    await session.run_sync(_guard)

    def _run(sync_session: Session) -> IndexingScanDTO:
        result = run_portfolio_scan(
            sync_session,
            trigger="manual",
            client_id=payload.client_id,
            full=payload.full,
            settings=settings,
            user_id=attribution.user_id,
        )
        repo = IndexingRepository(sync_session)
        scan = repo.get_scan(result.scan_id)
        names = repo.client_names()
        assert scan is not None
        return scan_dto(
            scan,
            client_name=names.get(scan.client_id) if scan.client_id else None,
        )

    # Скан ходить у Google по кілька хвилин — тримати на ньому event loop
    # означало б підвісити решту API.
    return await run_in_threadpool(_run_sync_scan, _run)


def _run_sync_scan(factory):  # type: ignore[no-untyped-def]
    """Скан бере ВЛАСНУ синхронну сесію, а не `run_sync` запиту.

    Він ходить у Google по кілька хвилин і комітить після кожного клієнта;
    тримати на цьому сесію HTTP-запиту означало б тримати відкриту
    транзакцію весь цей час.
    """
    from app.db.session import get_sync_sessionmaker

    maker = get_sync_sessionmaker()
    with maker() as sync_session:
        return factory(sync_session)


@router.get(
    "/scans/{scan_id}", response_model=IndexingScanDTO, responses={404: {"model": ErrorDTO}}
)
async def scan_status(scan_id: str, session: SessionDep) -> IndexingScanDTO:
    def _read(sync_session: Session) -> IndexingScanDTO:
        repo = IndexingRepository(sync_session)
        scan = repo.get_scan(scan_id)
        if scan is None:
            raise HTTPException(404, _error("scan_not_found", "No such scan."))
        names = repo.client_names()
        return scan_dto(
            scan, client_name=names.get(scan.client_id) if scan.client_id else None
        )

    return await session.run_sync(_read)


# --- аналітика ------------------------------------------------------------


@router.get("/analytics", response_model=IndexingAnalyticsDTO)
async def analytics(
    session: SessionDep, window_days: int = Query(default=180, ge=7, le=730)
) -> IndexingAnalyticsDTO:
    def _read(sync_session: Session) -> IndexingAnalyticsDTO:
        return build_analytics(sync_session, window_days=window_days)

    return await session.run_sync(_read)


@router.get("/export.xlsx")
async def export_workbook(
    session: SessionDep, client_id: str | None = None, window_days: int = 180
) -> StreamingResponse:
    """Черга + аналітика однією книгою, з тих самих DTO, що й екран."""

    def _read(sync_session: Session) -> bytes:
        from app.indexing.xlsx import build_workbook

        repo = IndexingRepository(sync_session)
        names = repo.client_names()
        settings_map = repo.settings_map()
        rows, total = repo.queue(
            client_ids=[client_id] if client_id else None, offset=0, limit=2000
        )
        contexts: dict[str, object] = {}
        items = []
        for row in rows:
            if row.client_id not in contexts:
                contexts[row.client_id] = load_client_context(
                    sync_session, client_id=row.client_id
                )
            settings = settings_map.get(row.client_id)
            items.append(
                url_dto(
                    row,
                    client_name=names.get(row.client_id, row.client_id),
                    context=contexts[row.client_id],  # type: ignore[arg-type]
                    resubmit_after_days=settings.resubmit_after_days if settings else 90,
                )
            )
        queue_dto = IndexingQueueDTO(urls=items, total=total, offset=0, limit=2000)
        return build_workbook(
            queue_dto, build_analytics(sync_session, window_days=window_days)
        )

    payload = await session.run_sync(_read)
    stamp = utcnow().date().isoformat()
    return StreamingResponse(
        iter([payload]),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": f'attachment; filename="indexing-planner-{stamp}.xlsx"'
        },
    )


register_router(router)
