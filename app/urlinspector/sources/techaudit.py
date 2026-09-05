"""Technical Audit (task-25) source for the URL Inspector (task-61 §2.3).

Findings are stored one row per `(check, URL)` under
`dataset_key="tech_findings"`, keyed by `dimension_value = "{slug}\x1f{url}"`
(task-25's own separator convention) — this module filters the whole
crawl's findings down to one URL in Python after narrowing by
`client_id + dataset_key` (indexed), same approach `app/api/tech_audit.py`
itself uses for its own per-category rollups.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.db.models import AnalyticsDatasetRow
from app.db.repositories.tech_audits import (
    FINDINGS_DATASET,
    TechAuditReadRepository,
    audit_provenance,
)
from app.schemas.url_inspector import UrlSourceStateDTO, UrlTechAuditDTO, UrlTechFindingDTO
from app.urlinspector.urlkey import lookup_variants


def _row_url(row: AnalyticsDatasetRow) -> str:
    _slug, _sep, url = row.dimension_value.partition("\x1f")
    return url


def collect_tech_audit(session: Session, *, client_id: str, url: str) -> UrlTechAuditDTO:
    read_repo = TechAuditReadRepository(session)
    snapshot = read_repo.latest_crawls([client_id]).get(client_id)
    if snapshot is None:
        state = UrlSourceStateDTO(
            source="tech_audit",
            state="no_data",
            detail="No technical audit has run for this client yet.",
        )
        return UrlTechAuditDTO(state=state)

    variants = set(lookup_variants(url))
    findings = read_repo.rows_for_crawl(snapshot, FINDINGS_DATASET)
    matches = [row for row in findings if _row_url(row) in variants]

    audit_date = str(audit_provenance(snapshot).get("audit_date") or "") or None
    dtos = [
        UrlTechFindingDTO(
            check=str(row.metrics_json.get("slug") or ""),
            category=str(row.metrics_json.get("category_name") or "") or None,
            severity=str(row.metrics_json.get("severity") or ""),
            detail=str(row.metrics_json.get("evidence") or "") or None,
            how_to_fix=str(row.metrics_json.get("how_to_fix") or "") or None,
        )
        for row in matches
    ]
    return UrlTechAuditDTO(
        state=UrlSourceStateDTO(source="tech_audit", state="has_data"),
        audit_date=audit_date,
        findings=dtos,
    )
