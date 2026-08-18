"""GA4 + GSC data via the Reporting Ninja API — the fixed attribution model
(task-42 §1.3/§3.2).

Ported from `AISeoTools/.../clientreport/{ga4,gsc}_fetcher.py` with the
finding from live measurement (§1.3) corrected, not carried over: the
original reads `sessionMedium` (session attribution) and sums Users/
Conversions across dimension rows, which disagreed with the PDF's own
"First user default channel grouping" table by 5-15% on organic/direct.
Here:

- totals (`users`, `conversions`) come from a query with NO dimension field
  at all, so they can never drift from a sum of dimension rows the provider
  buckets slightly differently;
- organic/direct come from a SEPARATE query dimensioned by
  `firstUserDefaultChannelGroup` (first-user attribution, matching the PDF),
  matched to the row whose channel is EXACTLY "Organic Search" or "Direct"
  — a substring match would also catch "Organic Social"/"Organic Shopping"
  rows, which task-42 §3.2 explicitly excludes ("Organic Search — тільки
  він").

`0` is a real, valid metric value everywhere here (PLAN §12) — only "no rows
came back at all" produces `None`. The ported `ga4_fetcher.py` had
`total_users or None`, which silently turned a genuine zero into "no data"
(task-42 §1.6); that line is not carried over.

task-45 §1.6/§2.5 adds the GSC discriminator this file was missing. Asked
WITHOUT a dimension, Reporting Ninja answers a period it has no data for with
one row of literal zeros (`{clicks: 0, impressions: 0}` for January 2023,
years before the property existed) — indistinguishable from a real zero, and
the reason `Month YOY` rendered `0` instead of `—`. Asked WITH the `date`
dimension, the same period answers with ZERO ROWS, and a period that does
have data answers with one row per day. So every GSC read here is
day-dimensioned and summed locally: no rows -> `None`, rows -> the true sum
(a genuinely zero day stays `0`).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from app.reports.models import PeriodMetrics
from app.reports.ninja_api import ReportingNinjaClient

GA4_INTEGRATION_ID = "ga4"
GSC_INTEGRATION_ID = "google_search_console"

_USERS_CANDIDATES = ("totalUsers", "activeUsers", "users")
_CONVERSIONS_CANDIDATES = ("conversions", "keyEvents", "goalCompletionsAll")
_CHANNEL_CANDIDATES = ("firstUserDefaultChannelGroup",)
_MONTH_CANDIDATES = ("yearMonth",)
_CLICKS_CANDIDATES = ("clicks",)
_IMPRESSIONS_CANDIDATES = ("impressions",)
_DATE_CANDIDATES = ("date",)

_ORGANIC_SEARCH = "organic search"
_DIRECT = "direct"

_GSC_WEB_SETTINGS = {"search_type": "web"}


def _row_limit(date_from: date, date_to: date, *, per_day: int = 1, floor: int = 100) -> int:
    """A limit that cannot silently truncate a day-dimensioned answer.

    `ReportingNinjaClient.query` defaults to `limit=100`, which is plenty for
    an undimensioned row or a handful of channel rows but would cut a
    day-dimensioned year (366 rows) down to 100 and produce a quietly WRONG
    sum. Sized from the window itself instead of a fixed constant, so widening
    `reports_history_months` can never reintroduce the truncation.
    """
    days = (date_to - date_from).days + 1
    return max(floor, days * per_day + 32)


class FieldDiscoveryError(ValueError):
    """A required GA4/GSC field could not be found in `list_fields()`."""


def _pick(available: set[str], candidates: tuple[str, ...]) -> str | None:
    return next((c for c in candidates if c in available), None)


def _int(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(float(str(value).replace(",", "").strip()))
    except (ValueError, TypeError):
        return None


def discover_ga4_fields(client: ReportingNinjaClient) -> dict[str, str]:
    available = {f["field_id"] for f in client.list_fields(GA4_INTEGRATION_ID)}
    users_field = _pick(available, _USERS_CANDIDATES)
    conversions_field = _pick(available, _CONVERSIONS_CANDIDATES)
    if not users_field or not conversions_field:
        raise FieldDiscoveryError(
            f"GA4 users/conversions field not found. Available: {sorted(available)[:15]}"
        )
    fields = {"users": users_field, "conversions": conversions_field}
    channel_field = _pick(available, _CHANNEL_CANDIDATES)
    if channel_field:
        fields["channel"] = channel_field
    month_field = _pick(available, _MONTH_CANDIDATES)
    if month_field:
        fields["month"] = month_field
    return fields


def discover_gsc_fields(client: ReportingNinjaClient) -> dict[str, str]:
    available = {f["field_id"] for f in client.list_fields(GSC_INTEGRATION_ID)}
    clicks_field = _pick(available, _CLICKS_CANDIDATES)
    impressions_field = _pick(available, _IMPRESSIONS_CANDIDATES)
    if not clicks_field or not impressions_field:
        raise FieldDiscoveryError(
            f"GSC clicks/impressions field not found. Available: {sorted(available)[:15]}"
        )
    fields = {"clicks": clicks_field, "impressions": impressions_field}
    date_field = _pick(available, _DATE_CANDIDATES)
    if date_field:
        fields["date"] = date_field
    return fields


def fetch_ga4_period(
    client: ReportingNinjaClient,
    connection_key: str,
    account_id: str,
    date_from: date,
    date_to: date,
    field_ids: dict[str, str],
) -> dict[str, int | None]:
    """`{users, organic_users, direct_users, conversions, organic_conversions,
    direct_conversions}` for one period, GA4 only.
    """
    users_field = field_ids["users"]
    conversions_field = field_ids["conversions"]
    channel_field = field_ids.get("channel")

    totals_rows = client.query(
        GA4_INTEGRATION_ID,
        connection_key,
        account_id,
        [users_field, conversions_field],
        date_from.isoformat(),
        date_to.isoformat(),
    )
    if not totals_rows:
        total_users: int | None = None
        total_conversions: int | None = None
    else:
        total_users = _int(totals_rows[0].get(users_field))
        total_conversions = _int(totals_rows[0].get(conversions_field))

    organic_users = organic_conversions = direct_users = direct_conversions = None
    if channel_field:
        rows = client.query(
            GA4_INTEGRATION_ID,
            connection_key,
            account_id,
            [channel_field, users_field, conversions_field],
            date_from.isoformat(),
            date_to.isoformat(),
        )
        organic_users, organic_conversions = _sum_exact_channel(
            rows, channel_field, users_field, conversions_field, _ORGANIC_SEARCH
        )
        direct_users, direct_conversions = _sum_exact_channel(
            rows, channel_field, users_field, conversions_field, _DIRECT
        )

    return {
        "users": total_users,
        "organic_users": organic_users,
        "direct_users": direct_users,
        "conversions": total_conversions,
        "organic_conversions": organic_conversions,
        "direct_conversions": direct_conversions,
    }


def _sum_exact_channel(
    rows: list[dict[str, object]],
    channel_field: str,
    users_field: str,
    conversions_field: str,
    target_channel: str,
) -> tuple[int | None, int | None]:
    """Sum `(users, conversions)` over rows whose channel EXACTLY matches
    `target_channel` (case-insensitive) — never a substring match, so
    "Organic Search" never also absorbs "Organic Social"/"Organic Shopping"
    (task-42 §3.2).
    """
    matched = [r for r in rows if str(r.get(channel_field, "")).strip().lower() == target_channel]
    if not matched:
        return None, None
    users = sum(_int(r.get(users_field)) or 0 for r in matched)
    conversions = sum(_int(r.get(conversions_field)) or 0 for r in matched)
    return users, conversions


@dataclass(frozen=True)
class GscDailyRow:
    """One GSC day. `clicks`/`impressions` are `None` only when the provider
    sent something unparseable — an actual zero-traffic day is `0`.
    """

    date: str  # YYYY-MM-DD
    clicks: int | None
    impressions: int | None


def fetch_gsc_daily(
    client: ReportingNinjaClient,
    connection_key: str,
    account_id: str,
    date_from: date,
    date_to: date,
    field_ids: dict[str, str],
) -> list[GscDailyRow]:
    """One row per DAY that GSC has data for, dates as `YYYY-MM-DD` strings
    exactly as the provider returned them.

    This is the §1.6 discriminator in raw form — an empty list means "no data
    in this window", not "zero clicks" — and the single query the monthly
    Historical Data block groups locally (task-45 §2.4).
    """
    date_field = field_ids["date"]
    clicks_field = field_ids["clicks"]
    impressions_field = field_ids["impressions"]
    rows = client.query(
        GSC_INTEGRATION_ID,
        connection_key,
        account_id,
        [date_field, clicks_field, impressions_field],
        date_from.isoformat(),
        date_to.isoformat(),
        limit=_row_limit(date_from, date_to),
        settings=dict(_GSC_WEB_SETTINGS),
    )
    return [
        GscDailyRow(
            date=str(row.get(date_field, "")).strip()[:10],
            clicks=_int(row.get(clicks_field)),
            impressions=_int(row.get(impressions_field)),
        )
        for row in rows
    ]


def fetch_gsc_period(
    client: ReportingNinjaClient,
    connection_key: str,
    account_id: str,
    date_from: date,
    date_to: date,
    field_ids: dict[str, str],
) -> dict[str, int | None]:
    """`{clicks, impressions}` for one period. `settings={"search_type":
    "web"}` matches the PDF's "Overall performance" table exactly (task-42
    §1.3: Clicks/Impressions already agreed 100% in live measurement).

    Day-dimensioned whenever the account exposes a `date` field (the normal
    case): zero rows -> `None`, so a period from before the property existed
    reads `—` instead of `0` (task-45 §1.6). The undimensioned query is kept
    only as the fallback for an account whose field list has no `date` at all
    — there it is still better than no data, and its zero-row case is handled
    the same way.
    """
    clicks_field = field_ids["clicks"]
    impressions_field = field_ids["impressions"]
    if "date" in field_ids:
        daily = fetch_gsc_daily(
            client, connection_key, account_id, date_from, date_to, field_ids
        )
        if not daily:
            return {"clicks": None, "impressions": None}
        return {
            "clicks": sum(row.clicks or 0 for row in daily),
            "impressions": sum(row.impressions or 0 for row in daily),
        }

    rows = client.query(
        GSC_INTEGRATION_ID,
        connection_key,
        account_id,
        [clicks_field, impressions_field],
        date_from.isoformat(),
        date_to.isoformat(),
        settings=dict(_GSC_WEB_SETTINGS),
    )
    if not rows:
        return {"clicks": None, "impressions": None}
    return {
        "clicks": sum(_int(r.get(clicks_field)) or 0 for r in rows),
        "impressions": sum(_int(r.get(impressions_field)) or 0 for r in rows),
    }


def fetch_ga4_monthly_channel_conversions(
    client: ReportingNinjaClient,
    connection_key: str,
    account_id: str,
    date_from: date,
    date_to: date,
    field_ids: dict[str, str],
    *,
    channel: str = _ORGANIC_SEARCH,
) -> dict[str, int]:
    """`{"2026-07": 654}` — key events for ONE channel, per calendar month.

    Dimensioned by `yearMonth` + `firstUserDefaultChannelGroup`: first-user
    attribution, the same model task-42 fixed the rest of the report to use.
    The ported `historical_fetcher.py` read organic via `sessionMedium`
    instead, which disagreed by 5-15% — carrying that over would make the
    history block's `Conversions` column contradict the `Organic Conversions`
    row of the very same workbook (task-45 §1.5).

    Months absent from the answer are absent from the dict — the caller turns
    that into `None`, never `0`.
    """
    month_field = field_ids["month"]
    channel_field = field_ids["channel"]
    conversions_field = field_ids["conversions"]
    rows = client.query(
        GA4_INTEGRATION_ID,
        connection_key,
        account_id,
        [month_field, channel_field, conversions_field],
        date_from.isoformat(),
        date_to.isoformat(),
        limit=_row_limit(date_from, date_to, floor=500),
    )
    per_month: dict[str, int] = {}
    for row in rows:
        if str(row.get(channel_field, "")).strip().lower() != channel:
            continue
        month = _normalize_year_month(row.get(month_field))
        if month is None:
            continue
        per_month[month] = per_month.get(month, 0) + (_int(row.get(conversions_field)) or 0)
    return per_month


def _normalize_year_month(value: object) -> str | None:
    """GA4's `yearMonth` comes back as `"202607"`; `"2026-07"` is what the rest
    of the engine speaks. Anything else is ignored rather than guessed at.
    """
    raw = str(value or "").strip()
    if len(raw) == 6 and raw.isdigit():
        return f"{raw[:4]}-{raw[4:]}"
    if len(raw) == 7 and raw[4] == "-" and raw[:4].isdigit() and raw[5:].isdigit():
        return raw
    return None


def fetch_period_metrics(
    client: ReportingNinjaClient,
    *,
    ga4_connection_key: str,
    ga4_account_id: str,
    ga4_fields: dict[str, str],
    gsc_ref: tuple[str, str] | None,
    gsc_fields: dict[str, str] | None,
    date_from: date,
    date_to: date,
) -> PeriodMetrics:
    """One period, both sources combined. `gsc_ref`/`gsc_fields` are `None`
    for a client without a `gsc` integration — `clicks`/`impressions` stay
    `None` (PLAN §12), never `0`.
    """
    ga4 = fetch_ga4_period(
        client, ga4_connection_key, ga4_account_id, date_from, date_to, ga4_fields
    )
    gsc: dict[str, int | None] = {"clicks": None, "impressions": None}
    if gsc_ref is not None and gsc_fields is not None:
        gsc_connection_key, gsc_account_id = gsc_ref
        gsc = fetch_gsc_period(
            client, gsc_connection_key, gsc_account_id, date_from, date_to, gsc_fields
        )
    return PeriodMetrics(
        users=ga4["users"],
        organic_users=ga4["organic_users"],
        direct_users=ga4["direct_users"],
        conversions=ga4["conversions"],
        organic_conversions=ga4["organic_conversions"],
        direct_conversions=ga4["direct_conversions"],
        clicks=gsc["clicks"],
        impressions=gsc["impressions"],
    )
