"""Site-level GSC query matching for the URL Inspector's keywords panel
(task-64 §3.3).

NOT per-page GSC data — Reporting Ninja has no page x query cross for GSC
(confirmed live, task-64 §0.2: a same-request `fields=["page","query",...]`
is rejected outright with `You can only use one dimension at a time`, and a
`{"filter": {"page": ...}}` body on a query-dimensioned request returns
`200 OK` with IDENTICAL rows for a real page and a nonexistent one — a
silent no-op, not a working filter). What we have instead: the SITE's own
top queries (`dimension='query'`), matched against this page's own text by
full token coverage. The caller-facing DTO and the frontend both carry a
fixed disclaimer next to this block — never presented as "this page's
queries".
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from app.urlkeywords.policy_shim import is_brand_query, topic_tokens

_DEFAULT_LIMIT = 15


class QueryRowLike(Protocol):
    dimension_value: str
    clicks: int | None
    impressions: int | None


@dataclass(frozen=True)
class SiteQueryMatch:
    query: str
    clicks: int | None
    impressions: int | None
    is_brand: bool


def match_site_queries(
    page_tokens: frozenset[str],
    query_rows: Sequence[QueryRowLike],
    *,
    brand_name: str,
    domain: str,
    limit: int = _DEFAULT_LIMIT,
) -> list[SiteQueryMatch]:
    """Site queries whose EVERY meaningful token is present on this page —
    full coverage only (not "any overlap"), so a query like "hearing aid
    price nyc" never matches a page that only ever mentions "hearing aid".
    """
    if not page_tokens:
        return []
    seen: set[str] = set()
    matches: list[SiteQueryMatch] = []
    for row in query_rows:
        query = (row.dimension_value or "").strip()
        key = query.lower()
        if not query or key in seen:
            continue
        seen.add(key)
        query_tokens = frozenset(topic_tokens(query))
        if not query_tokens or not query_tokens.issubset(page_tokens):
            continue
        matches.append(
            SiteQueryMatch(
                query=query,
                clicks=row.clicks,
                impressions=row.impressions,
                is_brand=is_brand_query(query, brand_name=brand_name, domain=domain),
            )
        )
    matches.sort(key=lambda m: (m.impressions or 0, m.clicks or 0), reverse=True)
    return matches[:limit]
