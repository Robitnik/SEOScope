"""Technical SEO Audit endpoints (task-25) — machine-only, no AI.

Read side (`latest` / `findings`) mirrors the Ahrefs Site Audit endpoints in
`analytics.py`: it never triggers collection, it just reads the newest stored
crawl. Write side (`runs` / `recheck`) mirrors `jobs.py`: it creates an ordinary
`SyncJob` for `Provider.tech_audit` and hands the caller the same
`/sync-jobs/{id}/events` SSE stream every other run uses, so live progress,
cancel and retry come for free.

Findings are immutable (`AnalyticsDatasetRow`), so a recheck never rewrites one:
it appends `tech_recheck` rows and the read side resolves the newest one per
finding. `export.xlsx` renders the company report template with the measured
`[П]` columns only — no agent copy, no QA gate.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.api import register_router
from app.api.deps import PaginationDep, SessionDep
from app.core.config import get_settings
from app.db.models import AnalyticsDatasetRow, ProviderSnapshot
from app.db.repositories.analytics_datasets import AnalyticsDatasetRepository
from app.db.repositories.clients import ClientRepository
from app.db.repositories.jobs import JobRepository
from app.db.session import worker_session_scope
from app.integrations.tech_audit import (
    MODE_CRAWL,
    MODE_RECHECK,
    RECHECK_SKIP_CATEGORIES,
    finding_key,
)
from app.schemas.api import (
    ErrorDTO,
    TechAuditCategoryDTO,
    TechAuditFindingDTO,
    TechAuditFindingsDTO,
    TechAuditRecheckRequest,
    TechAuditRecheckSummaryDTO,
    TechAuditRunAcceptedDTO,
    TechAuditSummaryDTO,
)
from app.schemas.enums import ComparisonMode, PeriodKind, Provider

router = APIRouter(prefix="/api/v1", tags=["tech-audit"])

_NOT_FOUND = ErrorDTO(error_code="client_not_found", message="Client not found").model_dump()
_NO_RUN = ErrorDTO(
    error_code="no_audit_run", message="This client has no technical audit run yet"
).model_dump()

CATEGORIES_DATASET = "tech_categories"
FINDINGS_DATASET = "tech_findings"
RECHECK_DATASET = "tech_recheck"


def _text(row: AnalyticsDatasetRow, key: str) -> str:
    return str(row.metrics_json.get(key) or "")


def _int(row: AnalyticsDatasetRow, key: str) -> int:
    try:
        return int(row.metrics_json.get(key) or 0)
    except (TypeError, ValueError):
        return 0


def _dict(row: AnalyticsDatasetRow, key: str) -> dict[str, Any]:
    value = row.metrics_json.get(key)
    return dict(value) if isinstance(value, dict) else {}


def _audit_provenance(snapshot: ProviderSnapshot | None) -> dict[str, Any]:
    """The crawl summary the adapter stored alongside its dataset rows."""
    if snapshot is None:
        return {}
    datasets = (snapshot.provenance_json or {}).get("analytics_datasets")
    return dict(datasets) if isinstance(datasets, dict) else {}


def _parse_dt(raw: Any) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw))
    except ValueError:
        return None


def _recheck_map(session: Session, client_id: str) -> dict[str, AnalyticsDatasetRow]:
    return AnalyticsDatasetRepository(session).latest_rows_by_dimension(
        client_id, Provider.tech_audit, RECHECK_DATASET
    )


# --- read ------------------------------------------------------------------


@router.get(
    "/clients/{client_id}/tech-audit/latest",
    response_model=TechAuditSummaryDTO,
    responses={404: {"model": ErrorDTO}},
)
async def get_tech_audit_latest(client_id: str, session: SessionDep) -> TechAuditSummaryDTO:
    """Categories + rollup of the newest technical audit crawl for this client."""

    def _run(s: Session) -> TechAuditSummaryDTO | None:
        if ClientRepository(s).get(client_id) is None:
            return None
        repo = AnalyticsDatasetRepository(s)
        categories = repo.latest(client_id, Provider.tech_audit, CATEGORIES_DATASET)
        if not categories:
            return TechAuditSummaryDTO(
                client_id=client_id, has_run=False, generated_at=datetime.now(UTC)
            )

        snapshot = s.get(ProviderSnapshot, categories[0].snapshot_id)
        prov = _audit_provenance(snapshot)

        # Roll the newest recheck verdicts up per category. Findings carry the
        # category, recheck rows carry only the finding key — join on that.
        findings = repo.latest(client_id, Provider.tech_audit, FINDINGS_DATASET)
        category_of = {row.dimension_value: _text(row, "category") for row in findings}
        rechecks: dict[str, TechAuditRecheckSummaryDTO] = {}
        for key, row in _recheck_map(s, client_id).items():
            category = category_of.get(key)
            if category is None:
                continue
            bucket = rechecks.setdefault(category, TechAuditRecheckSummaryDTO())
            status = _text(row, "status")
            if hasattr(bucket, status):
                setattr(bucket, status, getattr(bucket, status) + 1)

        return TechAuditSummaryDTO(
            client_id=client_id,
            has_run=True,
            run_id=categories[0].snapshot_id,
            observed_at=snapshot.observed_at if snapshot else None,
            audit_date=str(prov.get("audit_date") or "") or None,
            health_score=prov.get("health_score"),
            cms=str(prov.get("cms") or "") or None,
            pages_crawled=prov.get("pages_crawled"),
            quality=snapshot.quality.value if snapshot else None,
            totals=dict(prov.get("totals") or {}),
            warnings=list(snapshot.warnings_json or []) if snapshot else [],
            categories=[
                TechAuditCategoryDTO(
                    category=row.dimension_value,
                    name=_text(row, "name") or row.dimension_value,
                    severity=_text(row, "severity") or "Notice",
                    priority=_text(row, "priority") or "Low",
                    url_count=_int(row, "url_count"),
                    finding_count=_int(row, "finding_count"),
                    description=_text(row, "description"),
                    how_to_fix=_text(row, "how_to_fix"),
                    recheckable=row.dimension_value not in RECHECK_SKIP_CATEGORIES,
                    recheck=rechecks.get(row.dimension_value, TechAuditRecheckSummaryDTO()),
                )
                for row in categories
            ],
            generated_at=datetime.now(UTC),
        )

    result = await session.run_sync(_run)
    if result is None:
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    return result


@router.get(
    "/clients/{client_id}/tech-audit/findings",
    response_model=TechAuditFindingsDTO,
    responses={404: {"model": ErrorDTO}},
)
async def get_tech_audit_findings(
    client_id: str,
    session: SessionDep,
    page: PaginationDep,
    category: str | None = None,
    q: str | None = None,
) -> TechAuditFindingsDTO:
    """Per-URL findings of the newest crawl, with their latest recheck verdict."""

    def _run(s: Session) -> TechAuditFindingsDTO | None:
        if ClientRepository(s).get(client_id) is None:
            return None
        repo = AnalyticsDatasetRepository(s)
        rows = repo.latest(client_id, Provider.tech_audit, FINDINGS_DATASET)
        if category:
            rows = [r for r in rows if _text(r, "category") == category]
        if q:
            needle = q.strip().casefold()
            rows = [
                r
                for r in rows
                if needle
                in " ".join(
                    (_text(r, "url"), _text(r, "name"), _text(r, "current_value"))
                ).casefold()
            ]
        total = len(rows)
        window = rows[page.offset : page.offset + page.limit]
        rechecks = _recheck_map(s, client_id)

        out: list[TechAuditFindingDTO] = []
        for row in window:
            recheck = rechecks.get(row.dimension_value)
            recheck_status = _text(recheck, "status") or None if recheck is not None else None
            recheck_at = (
                _parse_dt(recheck.metrics_json.get("checked_at")) if recheck is not None else None
            )
            row_category = _text(row, "category")
            out.append(
                TechAuditFindingDTO(
                    id=row.dimension_value,
                    category=row_category,
                    category_name=_text(row, "category_name"),
                    slug=_text(row, "slug"),
                    name=_text(row, "name"),
                    severity=_text(row, "severity") or "Notice",
                    url=_text(row, "url"),
                    current_value=_text(row, "current_value"),
                    evidence=_text(row, "evidence"),
                    measured=_dict(row, "measured"),
                    how_to_fix=_text(row, "how_to_fix"),
                    final_url=_text(row, "final_url"),
                    live_status=_text(row, "live_status"),
                    recheck_status=recheck_status,
                    recheck_at=recheck_at,
                    recheckable=row_category not in RECHECK_SKIP_CATEGORIES,
                )
            )
        return TechAuditFindingsDTO(
            client_id=client_id,
            category=category,
            rows=out,
            total=total,
            limit=page.limit,
            offset=page.offset,
            generated_at=datetime.now(UTC),
        )

    result = await session.run_sync(_run)
    if result is None:
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    return result


# --- run / recheck ---------------------------------------------------------


def _accepted(job_id: str) -> TechAuditRunAcceptedDTO:
    status_url = f"/api/v1/sync-jobs/{job_id}"
    return TechAuditRunAcceptedDTO(
        job_id=job_id, status_url=status_url, events_url=f"{status_url}/events"
    )


def _create_audit_job(client_id: str, *, mode: str, params: dict[str, Any] | None) -> str | None:
    with worker_session_scope() as session:
        if ClientRepository(session).get(client_id) is None:
            return None
        jobs = JobRepository(session).create_run(
            client_ids=[client_id],
            providers=[Provider.tech_audit],
            # A crawl has no reporting period; these satisfy the job/context shape
            # and are ignored by the adapter (findings are point-in-time).
            period_kind=PeriodKind.month,
            comparison_mode=ComparisonMode.closed,
            anchor_date=datetime.now(UTC).date(),
            mode=mode,
            params=params,
        )
        return jobs[0].id if jobs else None


@router.post(
    "/clients/{client_id}/tech-audit/runs",
    status_code=202,
    response_model=TechAuditRunAcceptedDTO,
    responses={404: {"model": ErrorDTO}},
)
async def start_tech_audit(client_id: str) -> TechAuditRunAcceptedDTO:
    """Queue a full re-crawl of this client's site (worker picks it up)."""
    job_id = await run_in_threadpool(_create_audit_job, client_id, mode=MODE_CRAWL, params=None)
    if job_id is None:
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    return _accepted(job_id)


def _plan_recheck(client_id: str, wanted: list[tuple[str, str]]) -> dict[str, Any] | None:
    """Resolve the source run dir and drop targets that no crawl can re-verify."""
    with worker_session_scope() as session:
        if ClientRepository(session).get(client_id) is None:
            return None
        repo = AnalyticsDatasetRepository(session)
        findings = repo.latest(client_id, Provider.tech_audit, FINDINGS_DATASET)
        if not findings:
            return {"error": "no_run"}
        snapshot = session.get(ProviderSnapshot, findings[0].snapshot_id)
        run_dir = str(_audit_provenance(snapshot).get("run_dir") or "")
        if not run_dir:
            return {"error": "no_run_dir"}

        category_of = {row.dimension_value: _text(row, "category") for row in findings}
        known = set(category_of)
        targets: list[dict[str, str]] = []
        skipped: list[str] = []
        for slug, url in wanted:
            key = finding_key(slug, url)
            if key not in known:
                continue
            if category_of[key] in RECHECK_SKIP_CATEGORIES:
                skipped.append(key)
                continue
            targets.append({"slug": slug, "url": url})
        return {"run_dir": run_dir, "targets": targets, "skipped": skipped}


@router.post(
    "/clients/{client_id}/tech-audit/findings/recheck",
    status_code=202,
    response_model=TechAuditRunAcceptedDTO,
    responses={404: {"model": ErrorDTO}, 409: {"model": ErrorDTO}},
)
async def recheck_tech_audit_findings(
    client_id: str, body: TechAuditRecheckRequest
) -> TechAuditRunAcceptedDTO:
    """Re-verify specific findings against the live site, reusing the stored crawl."""
    wanted = [(f.slug, f.url) for f in body.findings if f.slug and f.url]
    if not wanted:
        raise HTTPException(
            status_code=409,
            detail=ErrorDTO(
                error_code="no_findings", message="No findings were given to re-check"
            ).model_dump(),
        )

    plan = await run_in_threadpool(_plan_recheck, client_id, wanted)
    if plan is None:
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    if plan.get("error"):
        raise HTTPException(status_code=404, detail=_NO_RUN)
    if not plan["targets"]:
        # Orphan/sitemap findings are site-graph scoped: judging them from a
        # partial crawl would be a guess, so we refuse instead of guessing.
        raise HTTPException(
            status_code=409,
            detail=ErrorDTO(
                error_code="needs_full_rerun",
                message=(
                    "These findings are site-graph scoped and can only be "
                    "re-verified by a full audit re-run"
                ),
                details={"skipped": plan["skipped"]},
            ).model_dump(),
        )

    job_id = await run_in_threadpool(
        _create_audit_job,
        client_id,
        mode=MODE_RECHECK,
        params={"run_dir": plan["run_dir"], "findings": plan["targets"]},
    )
    if job_id is None:
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    return _accepted(job_id)


# --- export ----------------------------------------------------------------


def _build_export() -> Any:
    """Lazy seam over the AISeoTools machine-only XLSX writer (tests patch this)."""
    from seo_audit.dashboard.tech_audit import build_tech_audit_xlsx

    return build_tech_audit_xlsx


def _resolve_run_dir(rel: str) -> Path:
    """Resolve a stored run dir under the artifacts root, refusing escapes."""
    root = Path(get_settings().artifacts_dir).resolve()
    resolved = (root / rel).resolve()
    if not resolved.is_relative_to(root):
        raise PermissionError(f"run dir escapes the artifacts root: {rel}")
    return resolved


def _export_xlsx(client_id: str) -> tuple[Path, str] | str:
    with worker_session_scope() as session:
        client = ClientRepository(session).get(client_id)
        if client is None:
            return "client_not_found"
        repo = AnalyticsDatasetRepository(session)
        findings = repo.latest(client_id, Provider.tech_audit, FINDINGS_DATASET)
        if not findings:
            return "no_run"
        snapshot = session.get(ProviderSnapshot, findings[0].snapshot_id)
        prov = _audit_provenance(snapshot)
        rel_run_dir = str(prov.get("run_dir") or "")
        client_name, client_domain = client.name, client.domain
        audit_date = str(prov.get("audit_date") or "")

    if not rel_run_dir:
        return "no_run"
    try:
        run_dir = _resolve_run_dir(rel_run_dir)
    except PermissionError:
        return "no_run"
    if not run_dir.is_dir():
        return "run_dir_missing"

    try:
        build = _build_export()
    except Exception:  # noqa: BLE001 - AISeoTools absent (CI/deploy)
        return "unavailable"

    stamp = audit_date or date.today().isoformat()
    slug = (client_name or "TECH").upper().replace(" ", "-").replace("/", "-")
    filename = f"{slug}_Tech-Audit_{stamp.replace('-', '')}.xlsx"
    out_path = run_dir / filename
    try:
        build(run_dir, out_path, client=client_name, domain=client_domain)
    except Exception:  # noqa: BLE001 - a broken export must not 500 the UI
        return "export_failed"
    return out_path, filename


@router.get(
    "/clients/{client_id}/tech-audit/export.xlsx",
    responses={404: {"model": ErrorDTO}, 503: {"model": ErrorDTO}},
)
async def export_tech_audit_xlsx(client_id: str) -> FileResponse:
    """The company multi-sheet report — measured columns only, no agent copy."""
    outcome = await run_in_threadpool(_export_xlsx, client_id)
    if outcome == "client_not_found":
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    if outcome in ("no_run", "run_dir_missing"):
        raise HTTPException(status_code=404, detail=_NO_RUN)
    if outcome == "unavailable":
        raise HTTPException(
            status_code=503,
            detail=ErrorDTO(
                error_code="unavailable",
                message="Report export is unavailable: AISeoTools is not installed here",
            ).model_dump(),
        )
    if outcome == "export_failed":
        raise HTTPException(
            status_code=503,
            detail=ErrorDTO(
                error_code="export_failed", message="Could not build the report file"
            ).model_dump(),
        )
    assert isinstance(outcome, tuple)
    path, filename = outcome
    return FileResponse(
        path,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=filename,
    )


register_router(router)
