"""Orchestration: `monitor_pages` -> build -> QA -> persist -> DTO
(task-57). The one module `app/api/client_pages.py` calls into — kept
importable/testable without FastAPI or a real HTTP request.

`findings_json` is always stored FULL/`strict` (§model docstring on
`PageInventoryItem`); `?profile=calibrated` is applied on READ by
`app.pageinventory.calibrate`, reconstructed from the stored rows rather
than re-running the QA engine on every request.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.contentqa.findings import Finding
from app.contentqa.rules import Profile as ContentQaProfile
from app.core.config import Settings
from app.db.base import utcnow
from app.db.models import Client, MonitorPage, PageInventoryItem
from app.db.repositories.monitor import MonitorRepository
from app.db.repositories.page_inventory import PageInventoryRepository
from app.pageinventory.calibrate import calibrate, cluster_summary
from app.pageinventory.inventory import build_inventory
from app.pageinventory.qa import FieldFinding, run_page_inventory_qa
from app.schemas.page_inventory import (
    BreadcrumbNodeDTO,
    DuplicateClusterDTO,
    FindingDTO,
    PageInventoryRowDTO,
    PageInventorySummaryDTO,
    PageInventoryTableDTO,
    QaProfile,
    Severity,
)

STALE_AFTER = timedelta(days=7)

BuildOutcome = str  # "ok" | "monitor_disabled" | "no_pages_yet"


def _finding_to_stored(field: str, finding: Finding) -> dict[str, Any]:
    return {
        "check": finding.check, "severity": finding.severity, "ref": finding.ref,
        "field": field, "detail": finding.detail, "value": finding.value,
    }


def _finding_from_stored(entry: dict[str, Any]) -> tuple[str, Finding]:
    return entry["field"], Finding(
        check=entry["check"], severity=entry["severity"], ref=entry["ref"],
        detail=entry["detail"], value=entry.get("value"),
    )


def rebuild(session: Session, client_id: str, settings: Settings) -> BuildOutcome:
    """Rebuilds and persists the one active snapshot for `client_id` from
    whatever the monitor has ALREADY collected (task-57 §2.2 — never
    fetches the site itself; that's what `Re-crawl` triggers, via the
    monitor's own `POST .../monitor/runs?kind=full`, task-57 §2.2a)."""
    monitor_repo = MonitorRepository(session)
    monitor_settings = monitor_repo.get_settings(client_id)
    if monitor_settings is not None and not monitor_settings.enabled:
        return "monitor_disabled"

    build = build_inventory(session, client_id, artifacts_dir=Path(settings.artifacts_dir))
    if build.pages_total == 0:
        return "no_pages_yet"

    client = session.get(Client, client_id)
    brand_names = (client.name,) if client and client.name else ()
    qa_profile = ContentQaProfile(
        kind="page", intent="commercial", locale="en-US", brand_names=brand_names
    )
    qa_result = run_page_inventory_qa(build.rows, qa_profile)

    # "as-of" freshness (task-57 §2.2): the newest `last_checked_at` among
    # this client's monitor pages. Rows are lightweight dataclasses without
    # that timestamp (task-57 §3 stores it at the snapshot level only), so
    # it's read straight from `monitor_pages` here.
    latest_checked = session.execute(
        select(func.max(MonitorPage.last_checked_at)).where(MonitorPage.client_id == client_id)
    ).scalar_one_or_none()

    warnings: list[str] = []
    if build.not_checked_count:
        warnings.append(f"{build.not_checked_count} page(s) not checked by the monitor yet.")

    items_payload: list[dict[str, Any]] = []
    for row in build.rows:
        field_findings = qa_result.per_row.get(row.url, [])
        items_payload.append({
            "url": row.url,
            "canonical_key": row.canonical_key,
            "duplicate_urls_json": list(row.duplicate_urls),
            "page_label": row.page_label,
            "breadcrumbs_json": [{"name": n, "url": u} for n, u in row.breadcrumbs],
            "breadcrumb_source": row.breadcrumb_source,
            "title": row.title,
            "description": row.description,
            "keywords_json": list(row.keywords),
            "keywords_source": row.keywords_source,
            "h1": row.h1,
            "status": row.status,
            "status_label": row.status_label,
            "indexable": row.indexable,
            "canonical_missing": row.canonical_missing,
            "checked": row.checked,
            "findings_json": [_finding_to_stored(ff.field, ff.finding) for ff in field_findings],
        })

    repo = PageInventoryRepository(session)
    repo.replace_snapshot(
        client_id,
        snapshot_fields={
            "source_collected_at": latest_checked,
            "pages_total": build.pages_total,
            "pages_unique": build.pages_unique,
            "pages_with_breadcrumbs": build.pages_with_breadcrumbs,
            "pages_with_keywords": build.pages_with_keywords,
            "breadcrumb_source": build.breadcrumb_source_summary,
            "warnings_json": warnings,
        },
        items=items_payload,
    )
    repo.commit()
    return "ok"


def _row_dto(
    item: PageInventoryItem, calibrated_findings: list[FieldFinding]
) -> PageInventoryRowDTO:
    by_field: dict[str, list[FindingDTO]] = {}
    dtos: list[FindingDTO] = []
    worst: Severity | None = None
    order = {"error": 0, "warn": 1, "info": 2}
    for ff in calibrated_findings:
        dto = FindingDTO(
            check=ff.finding.check, severity=ff.finding.severity,
            field=ff.field, detail=ff.finding.detail, value=ff.finding.value,
        )
        dtos.append(dto)
        by_field.setdefault(ff.field, []).append(dto)
        if worst is None or order[dto.severity] < order[worst]:
            worst = dto.severity

    return PageInventoryRowDTO(
        url=item.url,
        canonical_key=item.canonical_key,
        duplicate_urls=list(item.duplicate_urls_json or []),
        page_label=item.page_label,
        breadcrumbs=[
            BreadcrumbNodeDTO(name=n["name"], url=n.get("url"))
            for n in (item.breadcrumbs_json or [])
        ],
        breadcrumb_source=item.breadcrumb_source,  # type: ignore[arg-type]
        title=item.title,
        description=item.description,
        keywords=list(item.keywords_json or []),
        keywords_source=item.keywords_source,  # type: ignore[arg-type]
        h1=item.h1,
        status=item.status,
        status_label=item.status_label,  # type: ignore[arg-type]
        indexable=item.indexable,
        canonical_missing=item.canonical_missing,
        checked=item.checked,
        findings=dtos,
        findings_by_field=by_field,
        worst_severity=worst,
    )


def get_table(
    session: Session,
    client_id: str,
    *,
    profile: QaProfile = "calibrated",
    query: str | None = None,
    status: str | None = None,
    field: str | None = None,
    severity: str | None = None,
    page: int = 1,
    page_size: int = 50,
) -> PageInventoryTableDTO | None:
    repo = PageInventoryRepository(session)
    snapshot = repo.get_snapshot(client_id)
    if snapshot is None:
        return None
    items = repo.list_items(snapshot.id)

    # Reconstruct the full raw finding stream (needed for cluster
    # union-find) and each item's own `FieldFinding`s from stored JSON.
    per_row: dict[str, list[FieldFinding]] = {}
    raw_findings: list[Finding] = []
    seen_raw: set[tuple[str, str, str, str | None]] = set()
    for item in items:
        row_findings: list[FieldFinding] = []
        for entry in item.findings_json or []:
            f_field, finding = _finding_from_stored(entry)
            row_findings.append(FieldFinding(field=f_field, finding=finding))
            key = (finding.check, finding.ref, finding.detail, finding.value)
            if key not in seen_raw:
                seen_raw.add(key)
                raw_findings.append(finding)
        per_row[item.url] = row_findings

    calibrated_per_row, clusters = calibrate(per_row, raw_findings, profile=profile)
    cluster_stats = cluster_summary(clusters)

    severity_counts: dict[str, int] = {"error": 0, "warn": 0, "info": 0}
    for findings in calibrated_per_row.values():
        for ff in findings:
            severity_counts[ff.finding.severity] = severity_counts.get(ff.finding.severity, 0) + 1

    filtered_items = list(items)
    if query:
        needle = query.strip().lower()
        filtered_items = [
            item for item in filtered_items
            if needle in item.url.lower()
            or needle in (item.title or "").lower()
            or needle in (item.description or "").lower()
            or needle in item.page_label.lower()
        ]
    if status:
        filtered_items = [item for item in filtered_items if item.status_label == status]
    if field:
        filtered_items = [
            item for item in filtered_items
            if any(ff.field == field for ff in calibrated_per_row.get(item.url, []))
        ]
    if severity:
        filtered_items = [
            item for item in filtered_items
            if any(ff.finding.severity == severity for ff in calibrated_per_row.get(item.url, []))
        ]

    total = len(filtered_items)
    page = max(1, page)
    start = (page - 1) * page_size
    page_items = filtered_items[start:start + page_size]

    built_at = snapshot.built_at
    is_stale = bool(built_at and (utcnow() - built_at) > STALE_AFTER)

    summary = PageInventorySummaryDTO(
        client_id=client_id,
        built_at=snapshot.built_at,
        source_collected_at=snapshot.source_collected_at,
        pages_total=snapshot.pages_total,
        pages_unique=snapshot.pages_unique,
        pages_with_breadcrumbs=snapshot.pages_with_breadcrumbs,
        pages_with_keywords=snapshot.pages_with_keywords,
        breadcrumb_source=snapshot.breadcrumb_source,  # type: ignore[arg-type]
        duplicate_groups=cluster_stats["duplicate_groups"],
        pages_in_duplicate_groups=cluster_stats["pages_in_duplicate_groups"],
        severity_counts=severity_counts,  # type: ignore[arg-type]
        warnings=list(snapshot.warnings_json or []),
        never_built=False,
        stale=is_stale,
    )

    return PageInventoryTableDTO(
        summary=summary,
        rows=[_row_dto(item, calibrated_per_row.get(item.url, [])) for item in page_items],
        duplicate_clusters=[
            DuplicateClusterDTO(check=c.check, field=c.field, members=list(c.members))
            for c in clusters
        ],
        total=total,
        page=page,
        page_size=page_size,
        profile=profile,
    )
