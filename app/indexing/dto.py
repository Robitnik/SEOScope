"""Модель -> DTO для Indexing Planner (task-100 §2.4).

Окремо від роутера з однієї причини: тут живуть рішення «що саме показати
як відсутнє». Їх легко зламати непомітно, і кожне з них має бути в одному
місці, а не розсипане по семи ендпоінтах.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from app.db.base import utcnow
from app.db.models import Client, IndexingScan, IndexingSettings, IndexingSubmission, IndexingUrl
from app.db.repositories.indexing import UNDO_WINDOW, IndexingRepository
from app.indexing.context import ClientContext
from app.indexing.score import action_for
from app.schemas.indexing import (
    IndexingClientDTO,
    IndexingScanDTO,
    IndexingSettingsDTO,
    IndexingSitemapErrorDTO,
    IndexingSubmissionDTO,
    IndexingUrlDTO,
)


def settings_dto(row: IndexingSettings) -> IndexingSettingsDTO:
    return IndexingSettingsDTO(
        enabled=row.enabled,
        daily_quota=row.daily_quota,
        resubmit_after_days=row.resubmit_after_days,
        scan_mode=row.scan_mode,  # type: ignore[arg-type]
    )


def client_dto(
    client: Client,
    settings: IndexingSettings,
    *,
    site_url: str | None,
    counts: dict[str, int] | None,
    last_scan: IndexingScan | None,
) -> IndexingClientDTO:
    """`counts=None` -> усі лічильники `None`.

    Клієнт, якого жодного разу не сканували, показує «not scanned yet», а
    не «0 сторінок, 0 у індексі» — останнє виглядає як факт про сайт, тоді
    як це факт про нас (PLAN §12).
    """
    coverage: Decimal | None = None
    if counts and counts["urls_total"]:
        coverage = Decimal(counts["indexed"]) / Decimal(counts["urls_total"])
    return IndexingClientDTO(
        id=client.id,
        name=client.name,
        configured=site_url is not None,
        site_url=site_url,
        settings=settings_dto(settings),
        urls_total=counts["urls_total"] if counts else None,
        indexed=counts["indexed"] if counts else None,
        candidates=counts["candidates"] if counts else None,
        excluded=counts["excluded"] if counts else None,
        never_inspected=counts["never_inspected"] if counts else None,
        coverage_ratio=coverage,
        last_scan_at=last_scan.started_at if last_scan else None,
        last_scan_status=last_scan.status if last_scan else None,  # type: ignore[arg-type]
    )


def _cooldown_left(row: IndexingUrl, *, resubmit_after_days: int) -> int | None:
    if row.last_submitted_at is None:
        return None
    elapsed = (utcnow() - row.last_submitted_at).days
    left = resubmit_after_days - elapsed
    return left if left > 0 else None


def url_dto(
    row: IndexingUrl,
    *,
    client_name: str,
    context: ClientContext | None = None,
    resubmit_after_days: int = 90,
) -> IndexingUrlDTO:
    facts = context.get(row.url_key) if context is not None else None
    return IndexingUrlDTO(
        id=row.id,
        client_id=row.client_id,
        client_name=client_name,
        url=row.url,
        title=facts.title if facts else None,
        description=facts.description if facts else None,
        is_indexed=row.is_indexed,
        coverage_state=row.coverage_state,
        last_crawl_time=row.last_crawl_time,
        inspected_at=row.inspected_at,
        gsc_link=row.inspection_result_link,
        excluded=row.excluded,
        excluded_reason=row.excluded_reason,  # type: ignore[arg-type]
        reason_code=row.reason_code,  # type: ignore[arg-type]
        reason=dict(row.reason_json or {}),
        action=action_for(row.reason_code),  # type: ignore[arg-type]
        priority_score=row.priority_score,
        last_submitted_at=row.last_submitted_at,
        submissions_count=row.submissions_count or 0,
        cooldown_days_left=_cooldown_left(row, resubmit_after_days=resubmit_after_days),
        in_sitemap=row.gone_from_sitemap_at is None,
    )


def submission_dto(
    row: IndexingSubmission,
    url: IndexingUrl | None,
    *,
    client_name: str,
    submitted_by: str | None = None,
    title: str | None = None,
) -> IndexingSubmissionDTO:
    undoable = (
        row.status == "sent"
        and row.submitted_at is not None
        and utcnow() - row.submitted_at <= UNDO_WINDOW
    )
    return IndexingSubmissionDTO(
        id=row.id,
        client_id=row.client_id,
        client_name=client_name,
        indexing_url_id=row.indexing_url_id,
        url=url.url if url else "",
        planned_for=row.planned_for,
        status=row.status,  # type: ignore[arg-type]
        reason_code=row.reason_code,  # type: ignore[arg-type]
        reason=dict(row.reason_json or {}),
        state_at_plan=row.state_at_plan,
        action=action_for(row.reason_code),  # type: ignore[arg-type]
        submitted_at=row.submitted_at,
        submitted_by=submitted_by,
        state_at_submission=row.state_at_submission,
        indexed_at=row.indexed_at,
        days_to_index=row.days_to_index,
        note=row.note,
        undoable=undoable,
        gsc_link=url.inspection_result_link if url else None,
        title=title,
    )


def scan_dto(row: IndexingScan, *, client_name: str | None = None) -> IndexingScanDTO:
    return IndexingScanDTO(
        id=row.id,
        client_id=row.client_id,
        client_name=client_name,
        status=row.status,  # type: ignore[arg-type]
        trigger=row.trigger,  # type: ignore[arg-type]
        started_at=row.started_at,
        finished_at=row.finished_at,
        urls_total=row.urls_total,
        urls_inspected=row.urls_inspected,
        urls_failed=row.urls_failed,
        sitemap_added=row.sitemap_added,
        sitemap_gone=row.sitemap_gone,
        sitemap_errors=[
            IndexingSitemapErrorDTO(url=str(item.get("url", "")), error=str(item.get("error", "")))
            for item in (row.sitemap_errors_json or [])
        ],
        error_code=row.error_code,
        error_message=row.error_message,
    )


def titles_for(session: Session, *, client_id: str, url_keys: set[str]) -> dict[str, str]:
    """Заголовки з Page Inventory для показаних рядків, одним запитом."""
    if not url_keys:
        return {}
    from sqlalchemy import select

    from app.db.models import PageInventoryItem
    from app.urlinspector.urlkey import inspector_key

    rows = session.execute(
        select(PageInventoryItem.url, PageInventoryItem.title).where(
            PageInventoryItem.client_id == client_id
        )
    ).all()
    out: dict[str, str] = {}
    for url, title in rows:
        key = inspector_key(url)
        if key in url_keys and title:
            out[key] = title
    return out


def working_days_until(count: int, *, start: date) -> date:
    """Груба оцінка дати завершення черги в робочих днях."""
    remaining = count
    cursor = start
    while remaining > 0:
        cursor += timedelta(days=1)
        if cursor.weekday() < 5:
            remaining -= 1
    return cursor
