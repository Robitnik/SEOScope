"""Client Blogs endpoints (task-26) — machine-only, no AI.

Read side (`latest` / `posts`) mirrors the Technical SEO Audit endpoints in
`tech_audit.py`: it never triggers collection, it just reads the newest stored
crawl. Write side (`runs`) mirrors `jobs.py`: it creates an ordinary `SyncJob`
for `Provider.blogs` and hands the caller the same `/sync-jobs/{id}/events` SSE
stream every other run uses, so live progress, cancel and retry come for free.

`export.csv` / `export.xlsx` are built directly from the persisted `blog_posts`
rows — self-contained in the app (stdlib `csv` + `openpyxl`, already a direct
dependency, task-07), so both work even where AISeoTools is not installed.
"""

from __future__ import annotations

import csv
import io
from datetime import UTC, date, datetime

from fastapi import APIRouter, HTTPException
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse
from openpyxl import Workbook
from sqlalchemy.orm import Session

from app.api import register_router
from app.api.deps import PaginationDep, SessionDep
from app.db.models import AnalyticsDatasetRow, ProviderSnapshot
from app.db.repositories.analytics_datasets import AnalyticsDatasetRepository
from app.db.repositories.clients import ClientRepository
from app.db.repositories.jobs import JobRepository
from app.db.session import worker_session_scope
from app.integrations.blogs import MODE_CRAWL, POSTS_DATASET, SUMMARY_DATASET
from app.schemas.api import (
    BlogPostDTO,
    BlogPostsPageDTO,
    BlogsRunAcceptedDTO,
    BlogsSummaryDTO,
    ErrorDTO,
)
from app.schemas.enums import ComparisonMode, PeriodKind, Provider

router = APIRouter(prefix="/api/v1", tags=["blogs"])

_NOT_FOUND = ErrorDTO(error_code="client_not_found", message="Client not found").model_dump()
_NO_RUN = ErrorDTO(
    error_code="no_blogs_run", message="This client has no blog crawl run yet"
).model_dump()

_SORTS = ("date_desc", "date_asc", "words_desc", "words_asc", "title")


def _text(row: AnalyticsDatasetRow, key: str) -> str:
    return str(row.metrics_json.get(key) or "")


def _int_or_none(row: AnalyticsDatasetRow, key: str) -> int | None:
    value = row.metrics_json.get(key)
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _post_dto(row: AnalyticsDatasetRow) -> BlogPostDTO:
    return BlogPostDTO(
        url=_text(row, "url") or row.dimension_value,
        title=_text(row, "title"),
        description=_text(row, "description"),
        word_count=_int_or_none(row, "word_count"),
        published_date=row.metrics_json.get("published_date") or None,
        modified_date=row.metrics_json.get("modified_date") or None,
    )


def _sort_rows(rows: list[AnalyticsDatasetRow], sort: str) -> list[AnalyticsDatasetRow]:
    if sort == "date_asc":
        return sorted(rows, key=lambda r: _text(r, "published_date"))
    if sort == "words_desc":
        return sorted(rows, key=lambda r: _int_or_none(r, "word_count") or 0, reverse=True)
    if sort == "words_asc":
        return sorted(rows, key=lambda r: _int_or_none(r, "word_count") or 0)
    if sort == "title":
        return sorted(rows, key=lambda r: _text(r, "title").casefold())
    # date_desc (default): missing dates sort last, not first.
    return sorted(rows, key=lambda r: _text(r, "published_date") or "", reverse=True)


# --- read --------------------------------------------------------------------


@router.get(
    "/clients/{client_id}/blogs/latest",
    response_model=BlogsSummaryDTO,
    responses={404: {"model": ErrorDTO}},
)
async def get_blogs_latest(client_id: str, session: SessionDep) -> BlogsSummaryDTO:
    """Post count + blog URL of the newest blog crawl for this client."""

    def _run(s: Session) -> BlogsSummaryDTO | None:
        if ClientRepository(s).get(client_id) is None:
            return None
        repo = AnalyticsDatasetRepository(s)
        summary_rows = repo.latest(client_id, Provider.blogs, SUMMARY_DATASET)
        if not summary_rows:
            return BlogsSummaryDTO(
                client_id=client_id, has_run=False, generated_at=datetime.now(UTC)
            )

        summary = summary_rows[0]
        snapshot = s.get(ProviderSnapshot, summary.snapshot_id)
        return BlogsSummaryDTO(
            client_id=client_id,
            has_run=True,
            run_id=summary.snapshot_id,
            observed_at=snapshot.observed_at if snapshot else None,
            blog_url=_text(summary, "blog_url") or None,
            post_count=_int_or_none(summary, "post_count"),
            quality=snapshot.quality.value if snapshot else None,
            warnings=list(snapshot.warnings_json or []) if snapshot else [],
            generated_at=datetime.now(UTC),
        )

    result = await session.run_sync(_run)
    if result is None:
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    return result


@router.get(
    "/clients/{client_id}/blogs/posts",
    response_model=BlogPostsPageDTO,
    responses={404: {"model": ErrorDTO}},
)
async def get_blogs_posts(
    client_id: str,
    session: SessionDep,
    page: PaginationDep,
    q: str | None = None,
    sort: str = "date_desc",
) -> BlogPostsPageDTO:
    """Posts of the newest crawl, searchable and sortable."""
    if sort not in _SORTS:
        sort = "date_desc"

    def _run(s: Session) -> BlogPostsPageDTO | None:
        if ClientRepository(s).get(client_id) is None:
            return None
        repo = AnalyticsDatasetRepository(s)
        rows = repo.latest(client_id, Provider.blogs, POSTS_DATASET)
        if q:
            needle = q.strip().casefold()
            rows = [
                r
                for r in rows
                if needle in " ".join((_text(r, "title"), _text(r, "url"))).casefold()
            ]
        rows = _sort_rows(rows, sort)
        total = len(rows)
        window = rows[page.offset : page.offset + page.limit]
        return BlogPostsPageDTO(
            client_id=client_id,
            q=q,
            sort=sort,
            rows=[_post_dto(row) for row in window],
            total=total,
            limit=page.limit,
            offset=page.offset,
            generated_at=datetime.now(UTC),
        )

    result = await session.run_sync(_run)
    if result is None:
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    return result


# --- run ---------------------------------------------------------------------


def _create_blogs_job(client_id: str) -> str | None:
    with worker_session_scope() as session:
        if ClientRepository(session).get(client_id) is None:
            return None
        jobs = JobRepository(session).create_run(
            client_ids=[client_id],
            providers=[Provider.blogs],
            # A crawl has no reporting period; these satisfy the job/context shape
            # and are ignored by the adapter (posts are point-in-time).
            period_kind=PeriodKind.month,
            comparison_mode=ComparisonMode.closed,
            anchor_date=datetime.now(UTC).date(),
            mode=MODE_CRAWL,
            params=None,
        )
        return jobs[0].id if jobs else None


@router.post(
    "/clients/{client_id}/blogs/runs",
    status_code=202,
    response_model=BlogsRunAcceptedDTO,
    responses={404: {"model": ErrorDTO}},
)
async def start_blogs_run(client_id: str) -> BlogsRunAcceptedDTO:
    """Queue a full re-crawl of this client's blog (worker picks it up)."""
    job_id = await run_in_threadpool(_create_blogs_job, client_id)
    if job_id is None:
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    status_url = f"/api/v1/sync-jobs/{job_id}"
    return BlogsRunAcceptedDTO(
        job_id=job_id, status_url=status_url, events_url=f"{status_url}/events"
    )


# --- export --------------------------------------------------------------------


def _export_rows(client_id: str) -> list[AnalyticsDatasetRow] | str:
    with worker_session_scope() as session:
        if ClientRepository(session).get(client_id) is None:
            return "client_not_found"
        rows = AnalyticsDatasetRepository(session).latest(client_id, Provider.blogs, POSTS_DATASET)
        if not rows:
            return "no_run"
        # Force-load metrics_json while the session is open; the caller reads
        # them after the session scope has closed.
        return _sort_rows(rows, "date_desc")


def _client_slug(client_id: str) -> str:
    with worker_session_scope() as session:
        client = ClientRepository(session).get(client_id)
        return (client.name if client else client_id).upper().replace(" ", "-").replace("/", "-")


_CSV_HEADER = ("Title", "Description", "Word count", "URL", "Published date")


def _row_values(row: AnalyticsDatasetRow) -> tuple[str, str, int | str, str, str]:
    word_count = _int_or_none(row, "word_count")
    return (
        _text(row, "title"),
        _text(row, "description"),
        word_count if word_count is not None else "",
        _text(row, "url") or row.dimension_value,
        _text(row, "published_date"),
    )


@router.get(
    "/clients/{client_id}/blogs/export.csv",
    responses={404: {"model": ErrorDTO}},
)
async def export_blogs_csv(client_id: str) -> StreamingResponse:
    rows = await run_in_threadpool(_export_rows, client_id)
    if rows == "client_not_found":
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    if rows == "no_run":
        raise HTTPException(status_code=404, detail=_NO_RUN)
    assert isinstance(rows, list)

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(_CSV_HEADER)
    for row in rows:
        writer.writerow(_row_values(row))
    buffer.seek(0)

    slug = await run_in_threadpool(_client_slug, client_id)
    filename = f"{slug}_Blogs_{date.today().strftime('%Y%m%d')}.csv"
    return StreamingResponse(
        iter([buffer.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get(
    "/clients/{client_id}/blogs/export.xlsx",
    responses={404: {"model": ErrorDTO}},
)
async def export_blogs_xlsx(client_id: str) -> StreamingResponse:
    rows = await run_in_threadpool(_export_rows, client_id)
    if rows == "client_not_found":
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    if rows == "no_run":
        raise HTTPException(status_code=404, detail=_NO_RUN)
    assert isinstance(rows, list)

    wb = Workbook()
    ws = wb.active
    ws.title = "Blog posts"
    ws.append(_CSV_HEADER)
    for row in rows:
        ws.append(_row_values(row))
    for column_cells in ws.columns:
        length = max((len(str(c.value)) for c in column_cells if c.value is not None), default=10)
        ws.column_dimensions[column_cells[0].column_letter].width = min(length + 2, 80)

    buffer = io.BytesIO()
    wb.save(buffer)
    buffer.seek(0)

    slug = await run_in_threadpool(_client_slug, client_id)
    filename = f"{slug}_Blogs_{date.today().strftime('%Y%m%d')}.xlsx"
    return StreamingResponse(
        iter([buffer.getvalue()]),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


register_router(router)
