"""GSC dimensional analytics endpoints (PLAN §10/§21.4, task-16): per-page and
per-query clicks/impressions/CTR/position for a client, filtered by period.

Read-only: this router never triggers collection. It resolves the requested
period the same way every other endpoint does (`resolve_periods`) and looks
up the exact matching stored period in `gsc_dimension_metrics` — extended
periods (quarter/year/custom) that were never collected at that exact range
come back with an honest empty/`unavailable` answer rather than a fabricated
cross-period aggregate (out of this task's scope; see its Completion report).
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from sqlalchemy.orm import Session

from app.api import register_router
from app.api.deps import PaginationDep, SessionDep
from app.db.models import (
    AnalyticsDatasetRow,
    Client,
    DimensionKind,
    GscDimensionMetric,
    ProviderSnapshot,
)
from app.db.repositories.analytics_datasets import AnalyticsDatasetRepository
from app.db.repositories.clients import ClientRepository
from app.db.repositories.dimensions import DimensionRepository
from app.metrics import resolve_periods
from app.schemas.api import (
    AhrefsAffectedUrlDTO,
    AhrefsAffectedUrlsDTO,
    AhrefsIssueDTO,
    AhrefsSiteAuditDTO,
    AnalyticsDatasetResponseDTO,
    AnalyticsDatasetRowDTO,
    DimensionAnalyticsResponseDTO,
    DimensionRowDTO,
    ErrorDTO,
)
from app.schemas.enums import ComparisonMode, PeriodKind, PeriodRole, Provider, Quality

router = APIRouter(prefix="/api/v1", tags=["analytics"])

_NOT_FOUND = ErrorDTO(error_code="client_not_found", message="Client not found").model_dump()
SortKey = Literal["clicks", "impressions", "ctr", "position"]
DatasetRole = Literal["current", "previous"]

REPORTING_NINJA_DATASETS = frozenset(
    {
        "ga4_daily",
        "ga4_channels",
        "ga4_landing_pages",
        "ga4_events",
        "ga4_devices",
        "ga4_countries",
        "gbp_daily",
        "gbp_locations",
        "gbp_search_terms",
        "gbp_reviews",
        # task-89 §5.1: new datasets the direct Google API transport can
        # surface that Reporting Ninja's REST proxy never carried — same
        # endpoint/whitelist, since the row shape (dataset_key, dimension_key,
        # metrics_json) is identical regardless of source.
        "ga4_key_events",
        "gsc_search_types",
        "gsc_sitemaps",
    }
)

# task-89 §5.1: `AnalyticsDatasetRow` carries no `provider` filter in
# `for_period()` (client_id + dataset_key + period is enough — see
# `AnalyticsDatasetRepository.for_period`), so this endpoint's URL staying
# `/reporting-ninja/{dataset_key}` never blocked adding GSC-native datasets
# here. But the RESPONSE `provider` field must still name the real source
# (CLAUDE.md: "provider/metric named identically everywhere") — a GSC
# dataset reported as `reporting_ninja` would be a lie the UI repeats
# verbatim. Every dataset not listed here is still `reporting_ninja`
# (today's behaviour, unchanged).
_GSC_NATIVE_DATASETS = frozenset({"gsc_search_types", "gsc_sitemaps"})


def _dataset_provider(dataset_key: str) -> Provider:
    return Provider.gsc if dataset_key in _GSC_NATIVE_DATASETS else Provider.reporting_ninja


def _snapshot_transport(provenance_json: dict[str, Any], nested_key: str) -> str | None:
    """The transport that filled a snapshot's dataset/dimension rows
    (task-89 §5.2) — found live while wiring this in: a `current`-role
    snapshot is the shared KPI snapshot, whose provenance nests
    dataset/dimension info under `nested_key` (`"analytics_datasets"` or
    `"dimensions"`, `app/jobs/runner.py`); a `previous`-role snapshot is its
    OWN, separately created snapshot whose provenance is flat instead — both
    shapes are tried here rather than assuming one."""
    nested = provenance_json.get(nested_key)
    if isinstance(nested, dict):
        value = nested.get("transport")
        if value:
            return str(value)
    value = provenance_json.get("transport")
    return str(value) if value else None


def _text_metric(row: AnalyticsDatasetRow, key: str) -> str:
    return str(row.metrics_json.get(key) or "")


def _int_metric(row: AnalyticsDatasetRow, key: str) -> int:
    try:
        return int(row.metrics_json.get(key) or 0)
    except (TypeError, ValueError):
        return 0


def _details_metric(row: AnalyticsDatasetRow) -> dict[str, object]:
    details = row.metrics_json.get("details")
    return dict(details) if isinstance(details, dict) else {}


@router.get(
    "/clients/{client_id}/analytics/ahrefs/site-audit",
    response_model=AhrefsSiteAuditDTO,
    responses={404: {"model": ErrorDTO}},
)
async def get_ahrefs_site_audit(
    client_id: str,
    session: SessionDep,
    severity: Literal["Error", "Warning", "Notice"] | None = None,
    q: str | None = None,
) -> AhrefsSiteAuditDTO:
    """Latest complete live Ahrefs issue catalog for the client."""

    def _run(
        s: Session,
    ) -> tuple[list[AnalyticsDatasetRow], ProviderSnapshot | None] | None:
        if ClientRepository(s).get(client_id) is None:
            return None
        rows = AnalyticsDatasetRepository(s).latest(
            client_id, Provider.ahrefs, "ahrefs_issues"
        )
        snapshot = s.get(ProviderSnapshot, rows[0].snapshot_id) if rows else None
        return rows, snapshot

    result = await session.run_sync(_run)
    if result is None:
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    rows, snapshot = result
    all_rows = rows
    if severity:
        rows = [row for row in rows if _text_metric(row, "severity") == severity]
    if q:
        needle = q.strip().casefold()
        rows = [
            row
            for row in rows
            if needle
            in " ".join(
                (
                    row.dimension_value,
                    _text_metric(row, "name"),
                    _text_metric(row, "description"),
                    _text_metric(row, "how_to_fix"),
                )
            ).casefold()
        ]
    counts = {
        kind: sum(1 for row in all_rows if _text_metric(row, "severity") == kind)
        for kind in ("Error", "Warning", "Notice")
    }
    return AhrefsSiteAuditDTO(
        client_id=client_id,
        observed_at=snapshot.observed_at if snapshot else None,
        data_through=snapshot.data_through if snapshot else None,
        project_id=(snapshot.provenance_json or {}).get("project_id") if snapshot else None,
        issues=[
            AhrefsIssueDTO(
                slug=row.dimension_value,
                issue_id=_text_metric(row, "issue_id"),
                name=_text_metric(row, "name") or row.dimension_value,
                severity=_text_metric(row, "severity") or "Notice",
                priority=_text_metric(row, "priority") or "low",
                description=_text_metric(row, "description"),
                how_to_fix=_text_metric(row, "how_to_fix"),
                affected_urls=_int_metric(row, "affected_urls"),
                exported_rows=_int_metric(row, "exported_rows"),
            )
            for row in rows
        ],
        total_issue_types=len(all_rows),
        total_affected_rows=sum(_int_metric(row, "exported_rows") for row in all_rows),
        severity_counts=counts,
        generated_at=datetime.now(UTC),
    )


@router.get(
    "/clients/{client_id}/analytics/ahrefs/affected-urls",
    response_model=AhrefsAffectedUrlsDTO,
    responses={404: {"model": ErrorDTO}},
)
async def get_ahrefs_affected_urls(
    client_id: str,
    session: SessionDep,
    page: PaginationDep,
    issue_slug: str | None = None,
    q: str | None = None,
) -> AhrefsAffectedUrlsDTO:
    """Affected page rows, including every column present in Ahrefs' ZIP."""

    def _run(s: Session) -> tuple[list[AnalyticsDatasetRow], int] | None:
        if ClientRepository(s).get(client_id) is None:
            return None
        rows = AnalyticsDatasetRepository(s).latest(
            client_id, Provider.ahrefs, "ahrefs_affected_urls"
        )
        if issue_slug:
            rows = [row for row in rows if _text_metric(row, "issue_slug") == issue_slug]
        if q:
            needle = q.strip().casefold()
            rows = [
                row
                for row in rows
                if needle in _text_metric(row, "url").casefold()
                or needle in str(row.metrics_json.get("details") or {}).casefold()
            ]
        total = len(rows)
        return rows[page.offset : page.offset + page.limit], total

    result = await session.run_sync(_run)
    if result is None:
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    rows, total = result
    return AhrefsAffectedUrlsDTO(
        client_id=client_id,
        issue_slug=issue_slug,
        rows=[
            AhrefsAffectedUrlDTO(
                url=_text_metric(row, "url"),
                issue_slug=_text_metric(row, "issue_slug"),
                issue_name=_text_metric(row, "issue_name"),
                severity=_text_metric(row, "severity") or "Notice",
                details=_details_metric(row),
            )
            for row in rows
        ],
        total_rows_available=total,
        limit=page.limit,
        offset=page.offset,
        generated_at=datetime.now(UTC),
    )


def _anchor_datetime(anchor: date | None) -> datetime:
    if anchor is None:
        return datetime.now(UTC)
    return datetime.combine(anchor, datetime.min.time().replace(hour=12), tzinfo=UTC)


def _to_row_dto(row: GscDimensionMetric) -> DimensionRowDTO:
    return DimensionRowDTO(
        dimension_value=row.dimension_value,
        clicks=row.clicks,
        impressions=row.impressions,
        ctr=row.ctr,
        position=row.position,
        quality=row.quality,
        rank=row.rank,
    )


async def _get_dimension_analytics(
    client_id: str,
    session: SessionDep,
    page: PaginationDep,
    *,
    dimension: DimensionKind,
    period: PeriodKind,
    anchor: date | None,
    comparison_mode: ComparisonMode,
    custom_start: date | None,
    custom_end: date | None,
    sort_by: SortKey,
) -> DimensionAnalyticsResponseDTO:
    anchor_dt = _anchor_datetime(anchor)

    def _run(
        s: Session,
    ) -> tuple[Client, list[GscDimensionMetric], int, date, date, str | None] | None:
        client = ClientRepository(s).get(client_id)
        if client is None:
            return None
        periods = resolve_periods(
            period,
            anchor_dt,
            tz=client.timezone,
            mode=comparison_mode,
            custom_start=custom_start,
            custom_end=custom_end,
        )
        rows, total = DimensionRepository(s).for_period(
            client_id,
            dimension,
            periods.current.start,
            periods.current.end,
            sort_by=sort_by,
            limit=page.limit,
            offset=page.offset,
        )
        # task-89 §5.2: same transport lookup as the reporting-ninja dataset
        # endpoint — one snapshot per period, so the first row's snapshot
        # speaks for the whole page. This endpoint is always the CURRENT
        # role (`_get_dimension_analytics` has no `role` param), whose
        # provenance nests dimension info under `"dimensions"`
        # (`app/jobs/runner.py`) — see `_snapshot_transport`.
        transport: str | None = None
        if rows:
            snapshot = s.get(ProviderSnapshot, rows[0].snapshot_id)
            if snapshot is not None:
                transport = _snapshot_transport(snapshot.provenance_json, "dimensions")
        return client, rows, total, periods.current.start, periods.current.end, transport

    result = await session.run_sync(_run)
    if result is None:
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    _client, rows, total, period_start, period_end, transport = result

    # Truncation is a property of the stored period, not of this response's
    # own page: even a page with zero truncated rows in it must still say so
    # if the period it belongs to was cut (PLAN §12 — never quietly implied).
    truncated = any(r.quality == Quality.truncated for r in rows) if rows else False

    return DimensionAnalyticsResponseDTO(
        client_id=client_id,
        dimension=dimension.value,
        period_start=period_start if total else None,
        period_end=period_end if total else None,
        rows=[_to_row_dto(r) for r in rows],
        total_rows_available=total,
        truncated=truncated,
        limit=page.limit,
        offset=page.offset,
        generated_at=datetime.now(UTC),
        transport=transport,
    )


@router.get(
    "/clients/{client_id}/analytics/pages",
    response_model=DimensionAnalyticsResponseDTO,
    responses={404: {"model": ErrorDTO}},
)
async def get_page_analytics(
    client_id: str,
    session: SessionDep,
    page: PaginationDep,
    period: PeriodKind = PeriodKind.week,
    anchor: date | None = None,
    comparison_mode: ComparisonMode = ComparisonMode.closed,
    custom_start: date | None = None,
    custom_end: date | None = None,
    sort_by: SortKey = "clicks",
) -> DimensionAnalyticsResponseDTO:
    return await _get_dimension_analytics(
        client_id,
        session,
        page,
        dimension=DimensionKind.page,
        period=period,
        anchor=anchor,
        comparison_mode=comparison_mode,
        custom_start=custom_start,
        custom_end=custom_end,
        sort_by=sort_by,
    )


@router.get(
    "/clients/{client_id}/analytics/queries",
    response_model=DimensionAnalyticsResponseDTO,
    responses={404: {"model": ErrorDTO}},
)
async def get_query_analytics(
    client_id: str,
    session: SessionDep,
    page: PaginationDep,
    period: PeriodKind = PeriodKind.week,
    anchor: date | None = None,
    comparison_mode: ComparisonMode = ComparisonMode.closed,
    custom_start: date | None = None,
    custom_end: date | None = None,
    sort_by: SortKey = "clicks",
) -> DimensionAnalyticsResponseDTO:
    return await _get_dimension_analytics(
        client_id,
        session,
        page,
        dimension=DimensionKind.query,
        period=period,
        anchor=anchor,
        comparison_mode=comparison_mode,
        custom_start=custom_start,
        custom_end=custom_end,
        sort_by=sort_by,
    )


@router.get(
    "/clients/{client_id}/analytics/gsc/daily",
    response_model=AnalyticsDatasetResponseDTO,
    responses={404: {"model": ErrorDTO}},
)
async def get_gsc_daily_dataset(
    client_id: str,
    session: SessionDep,
    page: PaginationDep,
    period: PeriodKind = PeriodKind.week,
    role: DatasetRole = "current",
    anchor: date | None = None,
    comparison_mode: ComparisonMode = ComparisonMode.closed,
    custom_start: date | None = None,
    custom_end: date | None = None,
) -> AnalyticsDatasetResponseDTO:
    """Exact-period daily GSC clicks/impressions collected for the trend chart."""
    anchor_dt = _anchor_datetime(anchor)

    def _run(
        s: Session,
    ) -> tuple[list[AnalyticsDatasetRow], int, date, date] | None:
        client = ClientRepository(s).get(client_id)
        if client is None:
            return None
        periods = resolve_periods(
            period,
            anchor_dt,
            tz=client.timezone,
            mode=comparison_mode,
            custom_start=custom_start,
            custom_end=custom_end,
        )
        date_range = periods.current if role == "current" else periods.previous
        rows = AnalyticsDatasetRepository(s).for_period(
            client_id, "gsc_daily", date_range.start, date_range.end
        )
        rows.sort(key=lambda row: row.dimension_value)
        total = len(rows)
        return rows[page.offset : page.offset + page.limit], total, date_range.start, date_range.end

    result = await session.run_sync(_run)
    if result is None:
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    rows, total, period_start, period_end = result
    return AnalyticsDatasetResponseDTO(
        client_id=client_id,
        provider=Provider.gsc,
        dataset_key="gsc_daily",
        dimension_key="date",
        period_role=PeriodRole(role),
        period_start=period_start if total else None,
        period_end=period_end if total else None,
        rows=[
            AnalyticsDatasetRowDTO(
                dimension_value=row.dimension_value,
                metrics={
                    key: Decimal(value) if value is not None else None
                    for key, value in row.metrics_json.items()
                },
                quality=row.quality,
                rank=row.rank,
            )
            for row in rows
        ],
        total_rows_available=total,
        truncated=any(row.quality == Quality.truncated for row in rows),
        limit=page.limit,
        offset=page.offset,
        generated_at=datetime.now(UTC),
    )


@router.get(
    "/clients/{client_id}/analytics/reporting-ninja/{dataset_key}",
    response_model=AnalyticsDatasetResponseDTO,
    responses={404: {"model": ErrorDTO}},
)
async def get_reporting_ninja_dataset(
    client_id: str,
    dataset_key: str,
    session: SessionDep,
    page: PaginationDep,
    period: PeriodKind = PeriodKind.week,
    role: DatasetRole = "current",
    anchor: date | None = None,
    comparison_mode: ComparisonMode = ComparisonMode.closed,
    custom_start: date | None = None,
    custom_end: date | None = None,
    sort_by: str = "sessions",
    q: str | None = None,
) -> AnalyticsDatasetResponseDTO:
    if dataset_key not in REPORTING_NINJA_DATASETS:
        raise HTTPException(
            status_code=404,
            detail=ErrorDTO(
                error_code="dataset_not_found", message="Unknown Reporting Ninja dataset"
            ).model_dump(),
        )
    anchor_dt = _anchor_datetime(anchor)

    def _run(
        s: Session,
    ) -> tuple[list[AnalyticsDatasetRow], int, date, date, str | None] | None:
        client = ClientRepository(s).get(client_id)
        if client is None:
            return None
        periods = resolve_periods(
            period,
            anchor_dt,
            tz=client.timezone,
            mode=comparison_mode,
            custom_start=custom_start,
            custom_end=custom_end,
        )
        date_range = periods.current if role == "current" else periods.previous
        rows = AnalyticsDatasetRepository(s).for_period(
            client_id, dataset_key, date_range.start, date_range.end
        )
        # task-89 §5.2: the matched snapshot's own transport, read straight
        # off `provenance_json` — every row in one `for_period()` answer
        # shares one `snapshot_id` (one collection, one transport). `role`
        # (current/previous) picks a DIFFERENT snapshot with a DIFFERENT
        # provenance shape (`app/jobs/runner.py`) — see `_snapshot_transport`.
        transport: str | None = None
        if rows:
            snapshot = s.get(ProviderSnapshot, rows[0].snapshot_id)
            if snapshot is not None:
                transport = _snapshot_transport(snapshot.provenance_json, "analytics_datasets")
        if q:
            needle = q.strip().casefold()
            rows = [row for row in rows if needle in row.dimension_value.casefold()]
        rows.sort(
            key=lambda row: Decimal(row.metrics_json.get(sort_by) or 0),
            reverse=sort_by != "date",
        )
        total = len(rows)
        return (
            rows[page.offset : page.offset + page.limit], total,
            date_range.start, date_range.end, transport,
        )

    result = await session.run_sync(_run)
    if result is None:
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    rows, total, period_start, period_end, transport = result
    return AnalyticsDatasetResponseDTO(
        client_id=client_id,
        provider=_dataset_provider(dataset_key),
        dataset_key=dataset_key,
        dimension_key=rows[0].dimension_key if rows else None,
        period_role=PeriodRole(role),
        period_start=period_start if total else None,
        period_end=period_end if total else None,
        transport=transport,
        rows=[
            AnalyticsDatasetRowDTO(
                dimension_value=row.dimension_value,
                metrics={
                    key: Decimal(value) if value is not None else None
                    for key, value in row.metrics_json.items()
                },
                quality=row.quality,
                rank=row.rank,
            )
            for row in rows
        ],
        total_rows_available=total,
        truncated=any(row.quality == Quality.truncated for row in rows),
        limit=page.limit,
        offset=page.offset,
        generated_at=datetime.now(UTC),
    )


register_router(router)
