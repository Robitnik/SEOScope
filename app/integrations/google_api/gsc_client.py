"""GSC Search Analytics API client — the `gsc_daily` transport (task-83 §3.2/§4).

`POST /webmasters/v3/sites/{siteUrl}/searchAnalytics/query` with `dimensions:
["date"]`. `siteUrl` is the value already stored in
`client_integrations.config_json["account_id"]` (`sc-domain:example.com` or a
`https://…/` URL-prefix), URL-encoded whole.

Pagination is written even though a 400-day `["date"]` query is one page: the
moment a second dimension is added it silently truncates otherwise. The API's
hard ceiling is 16 months of history and `rowLimit=25000`.
"""

from __future__ import annotations

from collections.abc import Iterator
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
        never a fabricated zero row (PLAN §12).
        """
        by_day: dict[str, GscDailyRow] = {}
        for row in self._paged_rows(site_url, start, end, search_type):
            keys = row.get("keys") or []
            if not keys:
                continue
            day = str(keys[0]).strip()
            if not day:
                continue
            by_day[day] = GscDailyRow(
                date=day,
                clicks=_num(row.get("clicks")),
                impressions=_num(row.get("impressions")),
                ctr=_num(row.get("ctr")),
                position=_num(row.get("position")),
            )
        return [by_day[day] for day in sorted(by_day)]

    def _paged_rows(
        self, site_url: str, start: date, end: date, search_type: str
    ) -> Iterator[dict[str, Any]]:
        path = f"{_GSC_API_BASE}/sites/{quote(site_url, safe='')}/searchAnalytics/query"
        start_row = 0
        while True:
            payload = self._session.post_json(
                path,
                {
                    "startDate": start.isoformat(),
                    "endDate": end.isoformat(),
                    "dimensions": ["date"],
                    "type": search_type,
                    "rowLimit": _ROW_LIMIT,
                    "startRow": start_row,
                },
            )
            rows = payload.get("rows") or []
            yield from rows
            if len(rows) < _ROW_LIMIT:
                return
            start_row += _ROW_LIMIT


def _num(value: object) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
