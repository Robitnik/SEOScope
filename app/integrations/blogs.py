"""Client Blogs `ProviderAdapter` — machine-only, no AI (task-26).

Maps the AISeoTools machine-only wrapper (`seo_audit.dashboard.blogs`) onto the
app's collection contract. Like `tech_audit` (task-25), this adapter produces
**no metrics at all**: a blog inventory is a list of posts, so its whole payload
rides `collect_datasets()` as `AnalyticsDatasetRow`s under two dataset keys, and
`collect()` exists to run the crawl (the only hook that receives
`is_cancel_requested`) and describe the run.

One job kind — `SyncJob.mode == "blogs"` — crawls the client's whole site, keeps
only pages classified as blog posts, deduplicates by canonical URL, and writes
`blog_posts` + `blog_summary`. Every "Refresh data" click is a fresh, independent
crawl: there is no recheck/append-only concern here, unlike tech_audit's per-finding
recheck, so the read side always resolves the single newest snapshot.
"""

from __future__ import annotations

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

MODE_CRAWL = "blogs"

_ARTIFACT_SUBDIR = "blogs"

POSTS_DATASET = "blog_posts"
SUMMARY_DATASET = "blog_summary"


class BlogsUnavailable(RuntimeError):
    """AISeoTools is not importable in this environment (CI/deploy)."""


# --- lazy seams over AISeoTools (monkeypatched by tests) ---------------------


def _collect_blogs(**kwargs: Any) -> Any:
    import asyncio

    from seo_audit.dashboard.blogs import collect_blogs

    return asyncio.run(collect_blogs(**kwargs))


def _seo_audit_available() -> bool:
    try:
        import seo_audit.dashboard.blogs  # noqa: F401
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
                provider=Provider.blogs,
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


def _seed_url(context: CollectionContext) -> str:
    domain = str(context.integration_config.get("domain") or "").strip()
    if not domain:
        return ""
    return domain if "://" in domain else f"https://{domain}"


def post_key(url: str) -> str:
    """Stable `dimension_value` for one post — the URL itself (already deduped
    by canonical in `collect_blogs`, so this is unique within one snapshot)."""
    return url


class BlogsAdapter:
    """`ProviderAdapter` for `Provider.blogs` — a post list, never metrics."""

    provider = Provider.blogs

    def __init__(self) -> None:
        # `collect()` runs the crawl (it is the only hook given a cancel checker);
        # `collect_datasets()` then turns that same result into rows. The runner
        # calls them back-to-back on one cell, each on its own thread but strictly
        # sequentially (it joins before the next), so handing the payload over
        # keyed by the shared context object is safe — and far cheaper than
        # crawling the site twice. Same pattern as `TechAuditAdapter` (task-25).
        self._pending: dict[int, Any] = {}

    # --- preflight ---------------------------------------------------------

    def preflight(self, context: CollectionContext) -> PreflightResult:
        if not _seed_url(context):
            return PreflightResult(
                provider=self.provider,
                ok=False,
                reason_code="not_configured",
                message="client has no domain to crawl",
            )
        if not _seo_audit_available():
            return PreflightResult(
                provider=self.provider,
                ok=False,
                reason_code="unavailable",
                message="AISeoTools (seo_audit) is not installed in this environment",
            )
        return PreflightResult(provider=self.provider, ok=True, units_estimated=0)

    # --- collect -------------------------------------------------------------

    def collect(
        self,
        context: CollectionContext,
        emit: Callable[[ProgressEvent], None],
        is_cancel_requested: Callable[[], bool],
    ) -> CollectionResult:
        if is_cancel_requested():
            raise CancelledByAdapter("blogs: cancelled before the crawl started")

        url = _seed_url(context)
        settings = get_settings()
        rel_run_dir = f"{_ARTIFACT_SUBDIR}/{context.client_id}/{new_uuid()}"
        abs_run_dir = Path(settings.artifacts_dir) / rel_run_dir

        try:
            result = _collect_blogs(
                seed_url=url,
                client=str(context.integration_config.get("client_label") or ""),
                out_dir=abs_run_dir,
                blog_url=str(context.integration_config.get("blog_url") or "") or None,
                max_pages=int(context.integration_config.get("max_pages") or 2000),
                on_event=_bridge_emit(emit),
                is_cancelled=is_cancel_requested,
            )
        except CancelledByAdapter:
            raise
        except BaseException as exc:
            # The AISeoTools cancel signal is a BaseException by design (mirrors
            # tech_audit.TechAuditCancelled); every other failure becomes an
            # honest `unavailable` result, never a crash.
            if type(exc).__name__ == "BlogsCancelled":
                raise CancelledByAdapter("blogs: cancelled mid-crawl") from exc
            if isinstance(exc, Exception):
                return self._unavailable(
                    error_code="crawl_failed",
                    warnings=[f"Blog crawl failed: {type(exc).__name__}"],
                    provenance={"seed_url": url},
                )
            raise

        self._pending[id(context)] = result

        posts = list(getattr(result, "posts", []) or [])
        warnings = [str(w) for w in (getattr(result, "warnings", []) or [])]
        quality = Quality.partial if warnings else Quality.complete

        return CollectionResult(
            provider=self.provider,
            quality=quality,
            observed_at=getattr(result, "observed_at", None) or datetime.now(UTC),
            metrics=[],  # a blog inventory has no metric values, by design
            warnings=warnings,
            provenance={
                "aiseotools_source": "seo_audit.dashboard.blogs.collect_blogs",
                "seed_url": url,
                "run_dir": rel_run_dir,
                "post_count": len(posts),
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

    # --- datasets ------------------------------------------------------------

    def collect_datasets(
        self,
        context: CollectionContext,
        emit: Callable[[ProgressEvent], None],
    ) -> AnalyticsDatasetCollectionResult:
        result = self._pending.pop(id(context), None)
        observed_at = datetime.now(UTC)
        if result is None:
            return AnalyticsDatasetCollectionResult(
                provider=self.provider,
                quality=Quality.unavailable,
                observed_at=observed_at,
                warnings=["No blog inventory payload was produced by collect()"],
                error_code="no_payload",
            )

        start, end = context.current.start, context.current.end
        posts = list(getattr(result, "posts", []) or [])

        rows: list[AnalyticsDatasetValueResult] = [
            AnalyticsDatasetValueResult(
                dataset_key=SUMMARY_DATASET,
                dimension_key="summary",
                dimension_value="blog",
                period_role=PeriodRole.current,
                period_start=start,
                period_end=end,
                metrics={
                    "post_count": len(posts),
                    "blog_url": getattr(result, "blog_url", "") or "",
                    "crawl_completeness": getattr(result, "crawl_completeness", 1.0),
                    "warnings": list(getattr(result, "warnings", []) or []),
                },
                quality=Quality.complete,
                rank=0,
            )
        ]

        # `(dataset_key, dimension_value)` is UNIQUE per snapshot. `collect_blogs`
        # already dedupes by canonical URL, but this guard makes a duplicate
        # `dimension_value` unable to abort the whole persist transaction — the
        # exact failure class task-25 hit and fixed for `tech_findings`.
        seen: set[str] = set()
        rank = 0
        for post in posts:
            key = post_key(post.url)
            if key in seen:
                continue
            seen.add(key)
            rank += 1
            rows.append(
                AnalyticsDatasetValueResult(
                    dataset_key=POSTS_DATASET,
                    dimension_key="post",
                    dimension_value=key,
                    period_role=PeriodRole.current,
                    period_start=start,
                    period_end=end,
                    metrics={
                        "url": post.url,
                        "title": post.title,
                        "description": post.description,
                        "word_count": post.word_count,
                        "published_date": post.published_date,
                        "modified_date": post.modified_date,
                    },
                    quality=Quality.complete,
                    rank=rank,
                )
            )

        emit(
            ProgressEvent(
                provider=Provider.blogs,
                stage="persist",
                completed=len(posts),
                total=len(posts),
                percent=100,
                message=f"Blogs: {len(posts)} posts",
            )
        )
        return AnalyticsDatasetCollectionResult(
            provider=self.provider,
            quality=Quality.complete if posts else Quality.partial,
            observed_at=observed_at,
            rows=rows,
            # Crawl warnings are already reported by `collect()`; the runner
            # concatenates both lists onto the snapshot (task-25 lesson), so
            # repeating them here would show every warning twice in the UI.
            warnings=[],
            provenance={
                "datasets": [SUMMARY_DATASET, POSTS_DATASET],
                "post_count": len(posts),
                "blog_url": getattr(result, "blog_url", "") or "",
                "domain": getattr(result, "domain", "") or "",
            },
        )


register_adapter(BlogsAdapter())
