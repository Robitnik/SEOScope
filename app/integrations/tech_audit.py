"""Technical SEO Audit `ProviderAdapter` — machine-only, no AI (task-25).

Maps the AISeoTools machine-only wrapper (`seo_audit.dashboard.tech_audit`) onto
the app's collection contract. Unlike the four metric providers this adapter
produces **no metrics at all**: a technical audit is a set of *findings*, so its
whole payload rides `collect_datasets()` as `AnalyticsDatasetRow`s under three
dataset keys, and `collect()` exists to run the crawl (the only hook that
receives `is_cancel_requested`) and describe the run.

Two run kinds, selected by `CollectionContext.job_mode`:

- `tech_audit`          — crawl the client's whole site, detect, write
  `tech_categories` + `tech_findings`, and persist the run dir under the
  artifacts root so recheck/export can reuse the crawl without re-fetching.
- `tech_audit_recheck`  — re-verify only the `(slug, url)` pairs named in
  `job_params["findings"]` against the live site, writing `tech_recheck` rows.
  Append-only: findings themselves are immutable, so a recheck never rewrites
  them — the read side resolves the newest recheck row per finding.

Rendering (Playwright/Chromium) is deliberately OFF: the container has no
browser binary, and every category except Core Web Vitals comes from the
httpx crawl. Screenshot/visual findings are agent-authored and out of scope.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from app.core.config import get_settings
from app.db.base import new_uuid
from app.integrations.registry import register_adapter
from app.jobs.fake_adapter import CancelledByAdapter
from app.schemas.adapter import (
    CollectionContext,
    CollectionResult,
    PreflightResult,
    ProgressEvent,
)
from app.schemas.analytics_datasets import (
    AnalyticsDatasetCollectionResult,
    AnalyticsDatasetValueResult,
)
from app.schemas.enums import PeriodRole, Provider, Quality

if TYPE_CHECKING:
    # Annotation-only: `seo_audit` (AISeoTools) is an OPTIONAL runtime dependency,
    # absent on CI/deploy, so it must never be imported at module load or
    # `import app.main` breaks there. Runtime symbols resolve through the lazy
    # seams below (which tests monkeypatch).
    from seo_audit.dashboard.events import ProgressEvent as DashboardProgressEvent

# Job modes this adapter answers to (`SyncJob.mode` / `CollectionContext.job_mode`).
MODE_CRAWL = "tech_audit"
MODE_RECHECK = "tech_audit_recheck"

# Mirrors seo_audit.dashboard.tech_audit.RECHECK_SKIP_FAMILIES. Duplicated as a
# plain literal so the API can answer "is this finding recheckable?" without
# importing AISeoTools (absent on CI). Kept in sync by test_tech_audit.py.
RECHECK_SKIP_CATEGORIES = frozenset({"orphan", "sitemap", "visual_ux"})

_ARTIFACT_SUBDIR = "tech_audit"


class TechAuditUnavailable(RuntimeError):
    """AISeoTools is not importable in this environment (CI/deploy)."""


# --- lazy seams over AISeoTools (monkeypatched by tests) ---------------------


def _collect_tech_audit(**kwargs: Any) -> Any:
    import asyncio

    from seo_audit.dashboard.tech_audit import collect_tech_audit

    return asyncio.run(collect_tech_audit(**kwargs))


def _recheck_tech_audit(**kwargs: Any) -> Any:
    import asyncio

    from seo_audit.dashboard.tech_audit import recheck_tech_audit

    return asyncio.run(recheck_tech_audit(**kwargs))


def _seo_audit_available() -> bool:
    try:
        import seo_audit.dashboard.tech_audit  # noqa: F401
    except Exception:  # noqa: BLE001 - any import failure means "not available here"
        return False
    return True


def _bridge_emit(
    emit: Callable[[ProgressEvent], None],
) -> Callable[[DashboardProgressEvent], None]:
    """Adapt the AISeoTools dashboard `ProgressEvent` into the app's own type."""

    def _on_event(event: DashboardProgressEvent) -> None:
        emit(
            ProgressEvent(
                provider=Provider.tech_audit,
                stage=event.stage,
                completed=event.completed,
                total=event.total,
                percent=event.percent,
                level=event.level,
                message=event.message,
                details=event.details,
            )
        )

    return _on_event


def _audit_url(context: CollectionContext) -> str:
    return str(context.integration_config.get("domain") or "").strip()


def finding_key(slug: str, url: str) -> str:
    """Stable `dimension_value` shared by a finding and its recheck rows."""
    return f"{slug}\x1f{url}"


def split_finding_key(key: str) -> tuple[str, str]:
    slug, _, url = key.partition("\x1f")
    return slug, url


class TechAuditAdapter:
    """`ProviderAdapter` for `Provider.tech_audit` — findings, never metrics."""

    provider = Provider.tech_audit

    def __init__(self) -> None:
        # `collect()` runs the crawl (it is the only hook given a cancel checker);
        # `collect_datasets()` then turns that same result into rows. The runner
        # calls them back-to-back on one cell, each on its own thread but strictly
        # sequentially (it joins before the next), so handing the payload over
        # keyed by the shared context object is safe — and far cheaper than
        # crawling the site twice.
        self._pending: dict[int, Any] = {}
        self._lock = threading.Lock()

    # --- preflight ---------------------------------------------------------

    def preflight(self, context: CollectionContext) -> PreflightResult:
        if not _audit_url(context):
            return PreflightResult(
                provider=self.provider,
                ok=False,
                reason_code="not_configured",
                message="client has no domain to audit",
            )
        if not _seo_audit_available():
            return PreflightResult(
                provider=self.provider,
                ok=False,
                reason_code="unavailable",
                message="AISeoTools (seo_audit) is not installed in this environment",
            )
        return PreflightResult(provider=self.provider, ok=True, units_estimated=0)

    # --- collect -----------------------------------------------------------

    def collect(
        self,
        context: CollectionContext,
        emit: Callable[[ProgressEvent], None],
        is_cancel_requested: Callable[[], bool],
    ) -> CollectionResult:
        if is_cancel_requested():
            raise CancelledByAdapter("tech_audit: cancelled before the crawl started")

        if context.job_mode == MODE_RECHECK:
            return self._collect_recheck(context, emit)
        return self._collect_crawl(context, emit, is_cancel_requested)

    def _collect_crawl(
        self,
        context: CollectionContext,
        emit: Callable[[ProgressEvent], None],
        is_cancel_requested: Callable[[], bool],
    ) -> CollectionResult:
        url = _audit_url(context)
        settings = get_settings()
        requested_max_pages = int(
            (context.job_params or {}).get("max_pages")
            or context.integration_config.get("max_pages")
            or 1000
        )
        rel_run_dir = f"{_ARTIFACT_SUBDIR}/{context.client_id}/{new_uuid()}"
        abs_run_dir = Path(settings.artifacts_dir) / rel_run_dir

        try:
            result = _collect_tech_audit(
                url=url,
                client=str(context.integration_config.get("client_label") or ""),
                out_dir=abs_run_dir,
                render=False,
                max_pages=requested_max_pages,
                on_event=_bridge_emit(emit),
                is_cancelled=is_cancel_requested,
            )
        except CancelledByAdapter:
            raise
        except BaseException as exc:
            # The AISeoTools cancel signal is a BaseException by design (it has to
            # slip past the crawler's own `except Exception` render guard); every
            # other failure becomes an honest `unavailable` result, never a crash.
            if type(exc).__name__ == "TechAuditCancelled":
                raise CancelledByAdapter("tech_audit: cancelled mid-crawl") from exc
            if isinstance(exc, Exception):
                return self._unavailable(
                    error_code="crawl_failed",
                    warnings=[f"Technical audit crawl failed: {type(exc).__name__}"],
                    provenance={"audit_url": url},
                )
            raise

        with self._lock:
            self._pending[id(context)] = ("crawl", result, rel_run_dir)

        pages = int(getattr(result, "pages_crawled", 0) or 0)
        findings = list(getattr(result, "findings", []) or [])
        warnings = [str(w) for w in (getattr(result, "warnings", []) or [])]
        truncated = bool(getattr(result, "truncated", False))
        if pages == 0 and not findings:
            quality = Quality.unavailable
        elif truncated:
            quality = Quality.truncated
        elif warnings:
            quality = Quality.partial
        else:
            quality = Quality.complete

        return CollectionResult(
            provider=self.provider,
            quality=quality,
            observed_at=getattr(result, "observed_at", None) or datetime.now(UTC),
            metrics=[],  # a technical audit has no metric values, by design
            warnings=warnings,
            provenance={
                "aiseotools_source": "seo_audit.dashboard.tech_audit.collect_tech_audit",
                "audit_url": url,
                "run_dir": rel_run_dir,
                "render": False,
                "max_pages": requested_max_pages,
                "pages_discovered": getattr(result, "pages_discovered", 0) or 0,
                "truncated": truncated,
            },
        )

    def _collect_recheck(
        self, context: CollectionContext, emit: Callable[[ProgressEvent], None]
    ) -> CollectionResult:
        params = context.job_params or {}
        rel_run_dir = str(params.get("run_dir") or "")
        raw_targets = params.get("findings") or []
        targets = [
            (str(t.get("slug", "")), str(t.get("url", "")))
            for t in raw_targets
            if isinstance(t, dict) and t.get("slug") and t.get("url")
        ]
        if not rel_run_dir or not targets:
            return self._unavailable(
                error_code="recheck_not_targeted",
                warnings=["Recheck job carries no run dir or no findings to verify"],
            )

        settings = get_settings()
        abs_run_dir = Path(settings.artifacts_dir) / rel_run_dir
        if not abs_run_dir.is_dir():
            return self._unavailable(
                error_code="run_dir_missing",
                warnings=["The audit run this recheck refers to is no longer on disk"],
                provenance={"run_dir": rel_run_dir},
            )

        try:
            result = _recheck_tech_audit(
                run_dir=abs_run_dir,
                targets=targets,
                on_event=_bridge_emit(emit),
            )
        except Exception as exc:  # noqa: BLE001 - never crash the cell
            return self._unavailable(
                error_code="recheck_failed",
                warnings=[f"Recheck failed: {type(exc).__name__}"],
                provenance={"run_dir": rel_run_dir},
            )

        with self._lock:
            self._pending[id(context)] = ("recheck", result, rel_run_dir)

        rows = list(getattr(result, "rows", []) or [])
        return CollectionResult(
            provider=self.provider,
            quality=Quality.complete if rows else Quality.partial,
            observed_at=datetime.now(UTC),
            metrics=[],
            warnings=[] if rows else ["Recheck produced no rows"],
            provenance={
                "aiseotools_source": "seo_audit.dashboard.tech_audit.recheck_tech_audit",
                "run_dir": rel_run_dir,
                "rechecked": len(rows),
            },
        )

    def _unavailable(
        self,
        *,
        error_code: str,
        warnings: list[str],
        provenance: dict[str, Any] | None = None,
    ) -> CollectionResult:
        return CollectionResult(
            provider=self.provider,
            quality=Quality.unavailable,
            observed_at=datetime.now(UTC),
            metrics=[],
            warnings=warnings,
            error_code=error_code,
            provenance=provenance or {},
        )

    # --- datasets ----------------------------------------------------------

    def collect_datasets(
        self,
        context: CollectionContext,
        emit: Callable[[ProgressEvent], None],
    ) -> AnalyticsDatasetCollectionResult:
        with self._lock:
            pending = self._pending.pop(id(context), None)

        observed_at = datetime.now(UTC)
        if pending is None:
            return AnalyticsDatasetCollectionResult(
                provider=self.provider,
                quality=Quality.unavailable,
                observed_at=observed_at,
                warnings=["No technical audit payload was produced by collect()"],
                error_code="no_payload",
            )

        kind, result, rel_run_dir = pending
        if kind == "recheck":
            return self._recheck_datasets(context, result, rel_run_dir, observed_at)
        return self._crawl_datasets(context, result, rel_run_dir, observed_at, emit)

    def _crawl_datasets(
        self,
        context: CollectionContext,
        result: Any,
        rel_run_dir: str,
        observed_at: datetime,
        emit: Callable[[ProgressEvent], None],
    ) -> AnalyticsDatasetCollectionResult:
        start, end = context.current.start, context.current.end
        categories = list(getattr(result, "categories", []) or [])
        findings = list(getattr(result, "findings", []) or [])
        category_names = {c.category: c.name for c in categories}

        rows: list[AnalyticsDatasetValueResult] = []
        pages_crawled = int(getattr(result, "pages_crawled", 0) or 0)
        if pages_crawled == 0 and not findings:
            dataset_quality = Quality.unavailable
        elif bool(getattr(result, "truncated", False)):
            dataset_quality = Quality.truncated
        else:
            # A crawl that found zero issues is a successful, clean audit.  It
            # must remain `complete` so the card can truthfully show zero
            # instead of presenting an empty result as partial data.
            dataset_quality = Quality.complete
        for rank, cat in enumerate(categories, start=1):
            rows.append(
                AnalyticsDatasetValueResult(
                    dataset_key="tech_categories",
                    dimension_key="category",
                    dimension_value=cat.category,
                    period_role=PeriodRole.current,
                    period_start=start,
                    period_end=end,
                    metrics={
                        "name": cat.name,
                        "severity": cat.severity,
                        "priority": cat.priority,
                        "url_count": cat.url_count,
                        "finding_count": cat.finding_count,
                        "description": cat.description,
                        "how_to_fix": cat.how_to_fix,
                        "recheckable": cat.category not in RECHECK_SKIP_CATEGORIES,
                    },
                    quality=dataset_quality,
                    rank=rank,
                )
            )

        # `(dataset_key, dimension_value)` is UNIQUE per snapshot. A collision would
        # abort the whole persist transaction and lose the entire crawl — which is
        # exactly what a real run hit when one page belonged to two duplicate-title
        # groups. The producer now merges those, but this guard makes the failure
        # mode impossible rather than merely unlikely: drop the extra row, keep the run.
        seen: set[str] = set()
        rank = 0
        for f in findings:
            key = finding_key(f.slug, f.url)
            if key in seen:
                continue
            seen.add(key)
            rank += 1
            rows.append(
                AnalyticsDatasetValueResult(
                    dataset_key="tech_findings",
                    dimension_key="finding",
                    dimension_value=key,
                    period_role=PeriodRole.current,
                    period_start=start,
                    period_end=end,
                    metrics={
                        "slug": f.slug,
                        "category": f.category,
                        "category_name": category_names.get(f.category, f.category),
                        "name": f.name,
                        "severity": f.severity,
                        "url": f.url,
                        "current_value": f.current_value,
                        "evidence": getattr(f, "evidence", "") or "",
                        "measured": f.measured,
                        "how_to_fix": f.how_to_fix,
                        "final_url": f.final_url,
                        "live_status": f.live_status,
                        "recheckable": f.category not in RECHECK_SKIP_CATEGORIES,
                    },
                    quality=dataset_quality,
                    rank=rank,
                )
            )

        emit(
            ProgressEvent(
                provider=Provider.tech_audit,
                stage="persist",
                completed=len(rows),
                total=len(rows),
                percent=100,
                message=(
                    f"Technical audit: {len(categories)} categories, {len(findings)} findings"
                ),
            )
        )
        return AnalyticsDatasetCollectionResult(
            provider=self.provider,
            quality=dataset_quality,
            observed_at=observed_at,
            rows=rows,
            # Crawl warnings are already reported by `collect()`; the runner
            # concatenates both lists onto the snapshot, so repeating them here
            # showed every warning twice in the UI.
            warnings=[],
            provenance={
                "datasets": ["tech_categories", "tech_findings"],
                "run_dir": rel_run_dir,
                "audit_date": getattr(result, "audit_date", "") or "",
                "crawl_date": getattr(result, "crawl_date", "") or "",
                "health_score": getattr(result, "health_score", None),
                "cms": getattr(result, "cms", "") or "",
                "pages_crawled": getattr(result, "pages_crawled", 0) or 0,
                "pages_discovered": getattr(result, "pages_discovered", 0) or 0,
                "max_pages": getattr(result, "max_pages", 0) or 0,
                "truncated": bool(getattr(result, "truncated", False)),
                "totals": getattr(result, "totals", {}) or {},
                "domain": getattr(result, "domain", "") or "",
                "category_count": len(categories),
                "finding_count": len(findings),
            },
        )

    def _recheck_datasets(
        self,
        context: CollectionContext,
        result: Any,
        rel_run_dir: str,
        observed_at: datetime,
    ) -> AnalyticsDatasetCollectionResult:
        start, end = context.current.start, context.current.end
        checked_at = observed_at.isoformat()
        audit_run_id = str((context.job_params or {}).get("audit_run_id") or "")
        rows = [
            AnalyticsDatasetValueResult(
                dataset_key="tech_recheck",
                dimension_key="finding",
                dimension_value=finding_key(row.slug, row.url),
                period_role=PeriodRole.current,
                period_start=start,
                period_end=end,
                metrics={
                    "slug": row.slug,
                    "url": row.url,
                    "name": row.name,
                    "status": row.status,
                    "detail": row.detail,
                    "checked_at": checked_at,
                    "audit_run_id": audit_run_id,
                },
                quality=Quality.complete,
                rank=rank,
            )
            for rank, row in enumerate(getattr(result, "rows", []) or [], start=1)
        ]
        return AnalyticsDatasetCollectionResult(
            provider=self.provider,
            quality=Quality.complete if rows else Quality.partial,
            observed_at=observed_at,
            rows=rows,
            warnings=[],
            provenance={
                "datasets": ["tech_recheck"],
                "run_dir": rel_run_dir,
                "audit_run_id": audit_run_id,
                "rechecked": len(rows),
            },
        )


register_adapter(TechAuditAdapter())
