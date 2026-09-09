"""GSC Search Analytics API client — the `gsc_daily` transport (task-83
§3.2/§4), generalized to any dimension set for the client-page cutover
(task-89 §3.2).

`POST /webmasters/v3/sites/{siteUrl}/searchAnalytics/query`. `siteUrl` is the
value already stored in `client_integrations.config_json["account_id"]`
(`sc-domain:example.com` or a `https://…/` URL-prefix), URL-encoded whole.

Pagination is always written, even for a single-dimension query that fits in
one page: the moment a second dimension is added it silently truncates
otherwise. The API's hard ceiling is 16 months of history and
`rowLimit=25000` per page.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import quote

from app.integrations.google_api.auth import GoogleApiSession

_GSC_API_BASE = "https://www.googleapis.com/webmasters/v3"
_ROW_LIMIT = 25000


@dataclass(frozen=True)
class GscDailyRow:
    """One day of the Search Analytics `["date"]` series.

    `ctr` is the API's own fraction in `0..1` — it is NOT multiplied by 100.
    Reporting Ninja returns CTR as a percentage and `gsc.py` divides that by
    100; doing the same here would inflate every value ×100 while still
    drawing a plausible graph (task-83 §4/§7.3, the ×100 trap).
    """

    date: str  # YYYY-MM-DD
    clicks: Decimal | None
    impressions: Decimal | None
    ctr: Decimal | None
    position: Decimal | None


@dataclass(frozen=True)
class SitemapEntry:
    """One submitted sitemap, `GET .../sitemaps` (task-89 §4.5) — account
    state, not a date-range metric: no `type`/dimensions to request, no
    pagination (a site rarely has more than a handful of sitemaps).

    `last_downloaded`/`last_submitted` are the API's own ISO-8601 strings,
    kept raw here; the caller decides how to store them (task-89 turns them
    into Unix-epoch `Decimal`s, since `AnalyticsDatasetValueResult.metrics`
    is numeric-first everywhere else)."""

    path: str
    is_pending: bool
    errors: int
    warnings: int
    last_downloaded: str | None
    last_submitted: str | None
    # {"web": (submitted, indexed), "image": (submitted, indexed), ...}
    contents: dict[str, tuple[int | None, int | None]]


@dataclass(frozen=True)
class GscRow:
    """One row of an arbitrary-dimension Search Analytics query (task-89
    §3.2) — `keys[i]` lines up positionally with the `dimensions` the caller
    asked for (e.g. `dimensions=["page","query"]` -> `keys=(page, query)`).

    Same ×100 rule as `GscDailyRow.ctr`: never multiplied here.
    """

    keys: tuple[str, ...]
    clicks: Decimal | None
    impressions: Decimal | None
    ctr: Decimal | None
    position: Decimal | None


class GscClient:
    def __init__(self, session: GoogleApiSession) -> None:
        self._session = session

    def daily_search_analytics(
        self,
        site_url: str,
        start: date,
        end: date,
        *,
        search_type: str = "web",
    ) -> list[GscDailyRow]:
        """The full `["date"]` series for `[start, end]`, oldest day first.

        A day the API does not return is simply absent from the result — it is
        never a fabricated zero row (PLAN §12). Thin wrapper over
        `search_analytics()` kept verbatim so task-83's callers/tests see no
        behaviour change (task-89 §3.2)."""
        by_day: dict[str, GscDailyRow] = {}
        for row in self.search_analytics(
            site_url, start, end, dimensions=["date"], search_type=search_type
        ):
            day = row.keys[0]
            by_day[day] = GscDailyRow(
                date=day,
                clicks=row.clicks,
                impressions=row.impressions,
                ctr=row.ctr,
                position=row.position,
            )
        return [by_day[day] for day in sorted(by_day)]

    def search_analytics(
        self,
        site_url: str,
        start: date,
        end: date,
        *,
        dimensions: Sequence[str],
        search_type: str = "web",
        row_limit: int | None = None,
        max_rows: int | None = None,
    ) -> list[GscRow]:
        """Any dimension combination (`["page"]`, `["query"]`,
        `["page","query"]`, `["device"]`, `["country"]`,
        `["searchAppearance"]`, `["date","page"]`, …) — same endpoint task-83
        already pins for `["date"]`.

        A row with an empty `keys` list is skipped (never happens for
        `web`/`image`/`video` types with dimensions set, but the API's `type`
        totals query sends `dimensions=[]` and returns one row with no
        `keys` — callers that want a bare total pass `dimensions=[]` and read
        the single returned row's metrics directly). `max_rows` bounds how
        many rows this call returns in total, independent of the API's own
        `rowLimit` per page — the truncation this implies is the caller's to
        surface (`Quality.truncated`), never silently absorbed here.

        `row_limit=None` means "use the module's `_ROW_LIMIT`" — read as a
        global at call time (not bound as a default-argument value), so a
        test's `monkeypatch.setattr(gsc_module, "_ROW_LIMIT", ...)` still
        takes effect.
        """
        effective_row_limit = row_limit if row_limit is not None else _ROW_LIMIT
        out: list[GscRow] = []
        for raw in self._paged_rows(
            site_url, start, end, search_type, list(dimensions), effective_row_limit
        ):
            keys = tuple(str(k) for k in (raw.get("keys") or []))
            out.append(
                GscRow(
                    keys=keys,
                    clicks=_num(raw.get("clicks")),
                    impressions=_num(raw.get("impressions")),
                    ctr=_num(raw.get("ctr")),
                    position=_num(raw.get("position")),
                )
            )
            if max_rows is not None and len(out) >= max_rows:
                break
        return out

    def _paged_rows(
        self,
        site_url: str,
        start: date,
        end: date,
        search_type: str,
        dimensions: list[str],
        row_limit: int,
    ) -> Iterator[dict[str, Any]]:
        path = f"{_GSC_API_BASE}/sites/{quote(site_url, safe='')}/searchAnalytics/query"
        start_row = 0
        while True:
            payload = self._session.post_json(
                path,
                {
                    "startDate": start.isoformat(),
                    "endDate": end.isoformat(),
                    "dimensions": dimensions,
                    "type": search_type,
                    "rowLimit": row_limit,
                    "startRow": start_row,
                },
            )
            rows = payload.get("rows") or []
            yield from rows
            if len(rows) < row_limit:
                return
            start_row += row_limit

    def sitemaps(self, site_url: str) -> list[SitemapEntry]:
        """`GET .../sitemaps` — every sitemap submitted for this property
        (task-89 §4.5). One GET, no pagination (see `SitemapEntry`)."""
        path = f"{_GSC_API_BASE}/sites/{quote(site_url, safe='')}/sitemaps"
        payload = self._session.get_json(path)
        out: list[SitemapEntry] = []
        for raw in payload.get("sitemap") or []:
            contents: dict[str, tuple[int | None, int | None]] = {}
            for item in raw.get("contents") or []:
                content_type = str(item.get("type") or "").strip()
                if not content_type:
                    continue
                contents[content_type] = (_int(item.get("submitted")), _int(item.get("indexed")))
            out.append(
                SitemapEntry(
                    path=str(raw.get("path") or ""),
                    is_pending=bool(raw.get("isPending", False)),
                    errors=_int(raw.get("errors")) or 0,
                    warnings=_int(raw.get("warnings")) or 0,
                    last_downloaded=raw.get("lastDownloaded"),
                    last_submitted=raw.get("lastSubmitted"),
                    contents=contents,
                )
            )
        return out


def _num(value: object) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _int(value: object) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None
