"""Read model for the newest Technical Audit crawl and its own rechecks.

The generic analytics-dataset repository deliberately exposes snapshots and
rows without product semantics.  This module adds the one invariant a technical
audit needs: a recheck is allowed to affect only the crawl snapshot it was
created from.  New rows carry ``audit_run_id`` explicitly; legacy rows are
accepted only when their immutable ``run_dir`` matches the crawl.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import (
    AnalyticsDatasetRow,
    ProviderSnapshot,
    SyncJob,
    SyncJobProvider,
)
from app.schemas.enums import Provider

CATEGORIES_DATASET = "tech_categories"
FINDINGS_DATASET = "tech_findings"
RECHECK_DATASET = "tech_recheck"


def audit_provenance(snapshot: ProviderSnapshot | None) -> dict[str, Any]:
    if snapshot is None:
        return {}
    datasets = (snapshot.provenance_json or {}).get("analytics_datasets")
    return dict(datasets) if isinstance(datasets, dict) else {}


@dataclass(frozen=True)
class FindingCounts:
    open_errors: int
    error_urls: int
    open_warnings: int
    open_notices: int
    fixed_findings: int
    unreachable_findings: int
    total_findings: int


def count_findings(
    findings: Sequence[AnalyticsDatasetRow],
    rechecks: dict[str, AnalyticsDatasetRow],
) -> FindingCounts:
    open_by_severity = {"error": 0, "warning": 0, "notice": 0}
    error_urls: set[str] = set()
    fixed = 0
    unreachable = 0

    for row in findings:
        status = (
            str(rechecks[row.dimension_value].metrics_json.get("status") or "")
            if row.dimension_value in rechecks
            else ""
        )
        if status == "fixed":
            fixed += 1
            continue
        if status == "unreachable":
            # Unknown is still open.  It gets a separate count so the UI can
            # explain why it was not subtracted.
            unreachable += 1
        severity = str(row.metrics_json.get("severity") or "Notice").casefold()
        if severity not in open_by_severity:
            severity = "notice"
        open_by_severity[severity] += 1
        if severity == "error":
            url = str(row.metrics_json.get("url") or "")
            if url:
                error_urls.add(url)

    return FindingCounts(
        open_errors=open_by_severity["error"],
        error_urls=len(error_urls),
        open_warnings=open_by_severity["warning"],
        open_notices=open_by_severity["notice"],
        fixed_findings=fixed,
        unreachable_findings=unreachable,
        total_findings=len(findings),
    )


class TechAuditReadRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def latest_crawls(self, client_ids: Sequence[str]) -> dict[str, ProviderSnapshot]:
        ids = list(dict.fromkeys(client_ids))
        if not ids:
            return {}

        # Dataset membership is the durable discriminator between a full crawl
        # and a later recheck snapshot.  New zero-finding crawls also advertise
        # their dataset keys in provenance, so they remain visible.
        crawl_snapshot_ids = set(
            self._session.scalars(
                select(AnalyticsDatasetRow.snapshot_id)
                .where(
                    AnalyticsDatasetRow.client_id.in_(ids),
                    AnalyticsDatasetRow.provider == Provider.tech_audit,
                    AnalyticsDatasetRow.dataset_key.in_([CATEGORIES_DATASET, FINDINGS_DATASET]),
                )
                .distinct()
            )
        )
        snapshots = self._session.scalars(
            select(ProviderSnapshot)
            .where(
                ProviderSnapshot.client_id.in_(ids),
                ProviderSnapshot.provider == Provider.tech_audit,
            )
            .order_by(ProviderSnapshot.observed_at.desc(), ProviderSnapshot.created_at.desc())
        )
        out: dict[str, ProviderSnapshot] = {}
        for snapshot in snapshots:
            datasets = audit_provenance(snapshot).get("datasets") or []
            is_crawl = snapshot.id in crawl_snapshot_ids or FINDINGS_DATASET in datasets
            if is_crawl:
                out.setdefault(str(snapshot.client_id), snapshot)
        return out

    def findings_for_crawls(
        self, crawls: dict[str, ProviderSnapshot]
    ) -> dict[str, list[AnalyticsDatasetRow]]:
        snapshot_ids = [snapshot.id for snapshot in crawls.values()]
        out: dict[str, list[AnalyticsDatasetRow]] = {client_id: [] for client_id in crawls}
        if not snapshot_ids:
            return out
        rows = self._session.scalars(
            select(AnalyticsDatasetRow)
            .where(
                AnalyticsDatasetRow.snapshot_id.in_(snapshot_ids),
                AnalyticsDatasetRow.dataset_key == FINDINGS_DATASET,
            )
            .order_by(AnalyticsDatasetRow.rank.asc().nulls_last())
        )
        client_by_snapshot = {snapshot.id: client_id for client_id, snapshot in crawls.items()}
        for row in rows:
            client_id = client_by_snapshot.get(row.snapshot_id)
            if client_id is not None:
                out[client_id].append(row)
        return out

    def rows_for_crawl(
        self, snapshot: ProviderSnapshot, dataset_key: str
    ) -> list[AnalyticsDatasetRow]:
        return list(
            self._session.scalars(
                select(AnalyticsDatasetRow)
                .where(
                    AnalyticsDatasetRow.snapshot_id == snapshot.id,
                    AnalyticsDatasetRow.dataset_key == dataset_key,
                )
                .order_by(AnalyticsDatasetRow.rank.asc().nulls_last())
            )
        )

    def rechecks_for_crawls(
        self, crawls: dict[str, ProviderSnapshot]
    ) -> dict[str, dict[str, AnalyticsDatasetRow]]:
        out: dict[str, dict[str, AnalyticsDatasetRow]] = {client_id: {} for client_id in crawls}
        if not crawls:
            return out
        client_ids = list(crawls)
        rows = self._session.execute(
            select(AnalyticsDatasetRow, ProviderSnapshot)
            .join(ProviderSnapshot, ProviderSnapshot.id == AnalyticsDatasetRow.snapshot_id)
            .where(
                AnalyticsDatasetRow.client_id.in_(client_ids),
                AnalyticsDatasetRow.provider == Provider.tech_audit,
                AnalyticsDatasetRow.dataset_key == RECHECK_DATASET,
            )
            .order_by(AnalyticsDatasetRow.created_at.desc())
        )
        for row, recheck_snapshot in rows:
            client_id = str(row.client_id)
            crawl = crawls.get(client_id)
            if crawl is None or row.dimension_value in out[client_id]:
                continue
            source_run_id = str(row.metrics_json.get("audit_run_id") or "")
            if source_run_id:
                if source_run_id != crawl.id:
                    continue
            else:
                # Compatibility for rows written before audit_run_id existed.
                # A run dir is UUID-scoped, so equality is an unambiguous link.
                crawl_dir = str(audit_provenance(crawl).get("run_dir") or "")
                recheck_dir = str(audit_provenance(recheck_snapshot).get("run_dir") or "")
                if not crawl_dir or recheck_dir != crawl_dir:
                    continue
            out[client_id][row.dimension_value] = row
        return out

    def latest_crawl_attempts(
        self, client_ids: Sequence[str]
    ) -> dict[str, tuple[SyncJobProvider, SyncJob]]:
        ids = list(dict.fromkeys(client_ids))
        if not ids:
            return {}
        rows = self._session.execute(
            select(SyncJobProvider, SyncJob)
            .join(SyncJob, SyncJob.id == SyncJobProvider.job_id)
            .where(
                SyncJobProvider.client_id.in_(ids),
                SyncJobProvider.provider == Provider.tech_audit,
                SyncJob.mode == "tech_audit",
            )
            .order_by(SyncJobProvider.created_at.desc())
        )
        out: dict[str, tuple[SyncJobProvider, SyncJob]] = {}
        for cell, job in rows:
            out.setdefault(str(cell.client_id), (cell, job))
        return out
