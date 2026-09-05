"""Keywords + queries source for the URL Inspector (task-64 §3).

Two independently-sourced blocks:

1. On-page terms, extracted from the page's own last-crawled HTML
   (`MonitorPageVersion.body_blob_sha256`) — never a live fetch here, same
   "stored data only" contract as every other panel on this endpoint
   (task-61 §2.3). A real signal about THIS url's content.
2. Site-level GSC queries whose tokens are fully covered by this page's
   text. NOT per-page GSC data — see `app.urlkeywords.sitequeries`'s module
   docstring for the two live probes that ruled that out. Always labelled
   with its own `queries_state`, kept separate from the on-page `state`.

Both degrade to `no_data` (never a crash, never a fabricated empty-looking
list conflated with "nothing here") when `seo_audit.policy` isn't mounted —
`PolicyUnavailable` is caught locally so ONE missing dependency doesn't cost
the on-page block when only the query-matching half needed it, or vice
versa.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models import (
    DIMENSION_OTHER_VALUE,
    DimensionKind,
    GscDimensionMetric,
    MonitorPageVersion,
)
from app.db.repositories.clients import ClientRepository
from app.db.repositories.monitor import MonitorRepository
from app.monitor import storage as monitor_storage
from app.monitor.page import extract_signals
from app.schemas.url_inspector import (
    UrlKeywordsDTO,
    UrlKeywordTermDTO,
    UrlSiteQueryDTO,
    UrlSourceStateDTO,
)
from app.urlinspector.urlkey import lookup_variants
from app.urlkeywords.onpage import extract_on_page_terms
from app.urlkeywords.policy_shim import PolicyUnavailable, topic_tokens
from app.urlkeywords.sitequeries import match_site_queries

_QUERY_ROW_CAP = 500


def _latest_body_text(session: Session, page_id: str) -> str | None:
    blob_sha = session.execute(
        select(MonitorPageVersion.body_blob_sha256)
        .where(
            MonitorPageVersion.page_id == page_id,
            MonitorPageVersion.body_blob_sha256.is_not(None),
        )
        .order_by(MonitorPageVersion.observed_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    if blob_sha is None:
        return None
    settings = get_settings()
    try:
        raw = monitor_storage.read_blob(settings.artifacts_dir, blob_sha)
    except (FileNotFoundError, PermissionError, OSError):
        return None
    return raw.decode("utf-8", errors="replace")


def _latest_query_rows(session: Session, client_id: str) -> list[GscDimensionMetric]:
    """Rows of the most recently collected `query`-dimension snapshot for
    this client — same "last snapshot wins" rule as `build_series`, but
    site-wide rather than for one period, since this block isn't a time
    series. The top-N-cut remainder sentinel is excluded: it stands for
    many real queries bundled together, never one coherent phrase to match
    against a page's text.
    """
    latest_snapshot_id = session.scalar(
        select(GscDimensionMetric.snapshot_id)
        .where(
            GscDimensionMetric.client_id == client_id,
            GscDimensionMetric.dimension == DimensionKind.query,
        )
        .order_by(GscDimensionMetric.created_at.desc())
        .limit(1)
    )
    if latest_snapshot_id is None:
        return []
    stmt = (
        select(GscDimensionMetric)
        .where(
            GscDimensionMetric.snapshot_id == latest_snapshot_id,
            GscDimensionMetric.dimension == DimensionKind.query,
            GscDimensionMetric.dimension_value != DIMENSION_OTHER_VALUE,
        )
        .order_by(GscDimensionMetric.impressions.desc().nulls_last())
        .limit(_QUERY_ROW_CAP)
    )
    return list(session.scalars(stmt))


def collect_keywords(
    session: Session, *, client_id: str, url: str, meta_keywords: list[str]
) -> UrlKeywordsDTO:
    monitor_page = MonitorRepository(session).get_page_by_url_variants(
        client_id, lookup_variants(url)
    )

    page_tokens: frozenset[str] = frozenset()
    on_page_terms: list[UrlKeywordTermDTO] = []

    if monitor_page is None:
        on_page_state = UrlSourceStateDTO(
            source="keywords", state="no_data", detail="This URL isn't tracked by Site Monitor."
        )
    else:
        body_text = _latest_body_text(session, monitor_page.id)
        if body_text is None:
            on_page_state = UrlSourceStateDTO(
                source="keywords",
                state="no_data",
                detail="No crawled page body is stored for this URL yet.",
            )
        else:
            visible_text = extract_signals(body_text).visible_text
            try:
                terms = extract_on_page_terms(
                    visible_text,
                    title=monitor_page.title,
                    h1=monitor_page.h1,
                    description=monitor_page.description,
                )
                page_tokens = frozenset(topic_tokens(visible_text))
            except PolicyUnavailable as exc:
                on_page_state = UrlSourceStateDTO(
                    source="keywords", state="failed", detail=str(exc)
                )
                terms = []
            else:
                on_page_state = (
                    UrlSourceStateDTO(source="keywords", state="has_data")
                    if terms
                    else UrlSourceStateDTO(
                        source="keywords",
                        state="no_data",
                        detail="The stored page body has no extractable text.",
                    )
                )
            on_page_terms = [
                UrlKeywordTermDTO(
                    term=t.term,
                    count=t.count,
                    is_phrase=t.is_phrase,
                    in_title=t.in_title,
                    in_h1=t.in_h1,
                    in_description=t.in_description,
                )
                for t in terms
            ]

    query_rows = _latest_query_rows(session, client_id)
    site_queries: list[UrlSiteQueryDTO] = []
    if not query_rows:
        queries_state = UrlSourceStateDTO(
            source="site_queries",
            state="no_data",
            detail="No Search Console query data collected for this client yet.",
        )
    elif not page_tokens:
        queries_state = UrlSourceStateDTO(
            source="site_queries",
            state="no_data",
            detail="No page text to match queries against yet.",
        )
    else:
        client = ClientRepository(session).get(client_id)
        try:
            matches = match_site_queries(
                page_tokens,
                query_rows,
                brand_name=client.name if client else "",
                domain=client.domain if client else "",
            )
        except PolicyUnavailable as exc:
            queries_state = UrlSourceStateDTO(
                source="site_queries", state="failed", detail=str(exc)
            )
            matches = []
        else:
            queries_state = (
                UrlSourceStateDTO(source="site_queries", state="has_data")
                if matches
                else UrlSourceStateDTO(
                    source="site_queries",
                    state="no_data",
                    detail="No site query fully matches this page's text.",
                )
            )
        site_queries = [
            UrlSiteQueryDTO(
                query=m.query, clicks=m.clicks, impressions=m.impressions, is_brand=m.is_brand
            )
            for m in matches
        ]

    return UrlKeywordsDTO(
        state=on_page_state,
        on_page=on_page_terms,
        meta_keywords=list(meta_keywords),
        queries_state=queries_state,
        site_queries=site_queries,
    )
