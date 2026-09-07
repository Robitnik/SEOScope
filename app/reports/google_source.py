"""GA4 + GSC report metrics via the direct Google API — the same fixed
attribution model as `api_source.py`, one transport lower (task-84).

`api_source.py` reaches GSC and GA4 through Reporting Ninja's REST proxy;
this module reaches the same two Google APIs directly (task-83's transport
layer, `app.integrations.google_api`), with Reporting Ninja out of the chain
entirely. The attribution model is copied deliberately, because it is a
property of the model and not of the transport (task-84 §0.4):

- totals (`users`, `conversions`) come from a GA4 query with NO dimension at
  all, so they can never drift from a sum of dimension rows the provider
  buckets slightly differently;
- organic/direct come from a SEPARATE query dimensioned by
  `firstUserDefaultChannelGroup` (first-user attribution, matching the PDF),
  matched to the row whose channel is EXACTLY "Organic Search" or "Direct" —
  a substring match would also catch "Organic Social"/"Organic Shopping",
  which task-42 §3.2 excludes;
- `0` is a real, valid metric value everywhere (PLAN §12); only "no rows came
  back at all" produces `None`. `api_source.py`'s note about the ported
  `total_users or None` applies here too and is likewise not reproduced.

The GSC no-data discriminator `api_source.py` needs — Reporting Ninja answers
an empty period with one row of literal zeros unless it is asked by day — does
NOT apply here: the GSC Search Analytics API returns zero rows for an empty
period on its own (task-84 §0.4 point 4). `GscClient` still sends the
`["date"]` dimension, but only so the monthly history block can group by
month, not as a zero-vs-`None` guard.

Two GA4 traps, both inherited from task-83's transport notes: `conversions`
and `keyEvents` in one request error out (only `keyEvents` is ever asked
here — it is what `conversions` means), and the `month` dimension draws false
zeros (only `date` is used; month grouping is done locally).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date

from app.integrations.google_api import Ga4Client, Ga4Row, GoogleApiSession, GscClient
from app.reports.api_source import GscDailyRow
from app.reports.history import ReportHistory, build_history, month_keys, window_bounds
from app.reports.models import PeriodMetrics

_ORGANIC_SEARCH = "organic search"
_DIRECT = "direct"

_CHANNEL_DIMENSION = "firstUserDefaultChannelGroup"
# `totalUsers` + `keyEvents` share one request; `keyEvents` is `conversions`.
_USERS_METRIC = "totalUsers"
_CONVERSIONS_METRIC = "keyEvents"
_GSC_SEARCH_TYPE = "web"


def _int(value: object) -> int | None:
    """Same coercion as `api_source._int`: a real `0` survives, anything
    unparseable (including a missing metric) becomes `None`. Accepts the
    `Decimal` values the transport clients hand back as well as GA4's strings.
    """
    if value is None or value == "":
        return None
    try:
        return int(float(str(value).replace(",", "").strip()))
    except (ValueError, TypeError):
        return None


def _sum_exact_channel(
    rows: Sequence[Ga4Row], target_channel: str
) -> tuple[int | None, int | None]:
    """Sum `(users, conversions)` over rows whose channel EXACTLY matches
    `target_channel` (case-insensitive) — never a substring match, so
    "Organic Search" never also absorbs "Organic Social"/"Organic Shopping"
    (task-42 §3.2). No matching row at all -> `(None, None)`.
    """
    matched = [
        row
        for row in rows
        if str(row.dimensions.get(_CHANNEL_DIMENSION, "")).strip().lower() == target_channel
    ]
    if not matched:
        return None, None
    users = sum(_int(row.metrics.get(_USERS_METRIC)) or 0 for row in matched)
    conversions = sum(_int(row.metrics.get(_CONVERSIONS_METRIC)) or 0 for row in matched)
    return users, conversions


def fetch_ga4_period(
    session: GoogleApiSession, property_id: str, date_from: date, date_to: date
) -> dict[str, int | None]:
    """`{users, organic_users, direct_users, conversions, organic_conversions,
    direct_conversions}` for one period, GA4 only — the same six keys and the
    same two-query shape as `api_source.fetch_ga4_period`.
    """
    client = Ga4Client(session)

    totals = client.run_report(
        property_id, date_from, date_to, dimensions=[], metrics=[_USERS_METRIC, _CONVERSIONS_METRIC]
    )
    if not totals:
        total_users: int | None = None
        total_conversions: int | None = None
    else:
        total_users = _int(totals[0].metrics.get(_USERS_METRIC))
        total_conversions = _int(totals[0].metrics.get(_CONVERSIONS_METRIC))

    channel_rows = client.run_report(
        property_id,
        date_from,
        date_to,
        dimensions=[_CHANNEL_DIMENSION],
        metrics=[_USERS_METRIC, _CONVERSIONS_METRIC],
    )
    organic_users, organic_conversions = _sum_exact_channel(channel_rows, _ORGANIC_SEARCH)
    direct_users, direct_conversions = _sum_exact_channel(channel_rows, _DIRECT)

    return {
        "users": total_users,
        "organic_users": organic_users,
        "direct_users": direct_users,
        "conversions": total_conversions,
        "organic_conversions": organic_conversions,
        "direct_conversions": direct_conversions,
    }


def fetch_gsc_period(
    session: GoogleApiSession, site_url: str, date_from: date, date_to: date
) -> dict[str, int | None]:
    """`{clicks, impressions}` for one period, summed from the `["date"]`
    series. Zero rows -> `None` (the API's own empty-period answer), rows ->
    the true sum with a genuinely zero day staying `0`.
    """
    rows = GscClient(session).daily_search_analytics(
        site_url, date_from, date_to, search_type=_GSC_SEARCH_TYPE
    )
    if not rows:
        return {"clicks": None, "impressions": None}
    return {
        "clicks": sum(_int(row.clicks) or 0 for row in rows),
        "impressions": sum(_int(row.impressions) or 0 for row in rows),
    }


def fetch_period_metrics(
    session: GoogleApiSession,
    *,
    ga4_property_id: str,
    gsc_site_url: str | None,
    date_from: date,
    date_to: date,
) -> PeriodMetrics:
    """One period, both sources combined — the direct-transport mirror of
    `api_source.fetch_period_metrics`. `gsc_site_url` is `None` for a client
    without a `gsc` integration: `clicks`/`impressions` stay `None` (PLAN
    §12), never `0`.
    """
    ga4 = fetch_ga4_period(session, ga4_property_id, date_from, date_to)
    gsc: dict[str, int | None] = {"clicks": None, "impressions": None}
    if gsc_site_url:
        gsc = fetch_gsc_period(session, gsc_site_url, date_from, date_to)
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


def _monthly_organic_conversions(
    session: GoogleApiSession, property_id: str, window_from: date, window_to: date
) -> dict[str, int]:
    """`{"2026-07": 654}` — organic key events per calendar month, from a
    `[date, channel]` GA4 query grouped locally. task-83 §3.3 forbids the
    `month`/`yearMonth` dimension (it draws false zeros); the daily grain is
    grouped here the same way `app.metrics.portfolio` groups it for `#/trends`.
    A month absent from the answer is absent from the dict — the history
    builder turns that into `None`, never `0`.
    """
    rows = Ga4Client(session).run_report(
        property_id,
        window_from,
        window_to,
        dimensions=["date", _CHANNEL_DIMENSION],
        metrics=[_CONVERSIONS_METRIC],
    )
    per_month: dict[str, int] = {}
    for row in rows:
        if str(row.dimensions.get(_CHANNEL_DIMENSION, "")).strip().lower() != _ORGANIC_SEARCH:
            continue
        day = str(row.dimensions.get("date", "")).strip()
        if len(day) < 7 or day[4] != "-":
            continue
        month = day[:7]
        events = _int(row.metrics.get(_CONVERSIONS_METRIC)) or 0
        per_month[month] = per_month.get(month, 0) + events
    return per_month


def fetch_history(
    session: GoogleApiSession,
    *,
    ga4_property_id: str,
    gsc_site_url: str | None,
    months: int = 12,
    today: date | None = None,
) -> ReportHistory:
    """The monthly "Historical Data" block (task-45 §2.4) through the direct
    transport — the same `build_history` grouping `api_source`/`history.py`
    use, fed from Google directly instead of Reporting Ninja.

    A window that reaches past the GSC API's 16-month ceiling simply returns
    no day rows for those months, so they read `—` rather than `0` (task-84
    §0.7) — `build_history` already turns an absent month into `None`.
    """
    keys = month_keys(count=months, today=today)
    if not keys:
        return ReportHistory(months=[], has_gsc=gsc_site_url is not None, has_ga4=True)
    window_from, window_to = window_bounds(keys)

    has_gsc = gsc_site_url is not None
    daily_rows: list[GscDailyRow] = []
    if has_gsc and gsc_site_url is not None:
        daily_rows = [
            GscDailyRow(date=row.date, clicks=_int(row.clicks), impressions=_int(row.impressions))
            for row in GscClient(session).daily_search_analytics(
                gsc_site_url, window_from, window_to, search_type=_GSC_SEARCH_TYPE
            )
        ]

    monthly_conversions = _monthly_organic_conversions(
        session, ga4_property_id, window_from, window_to
    )
    return build_history(
        keys=keys,
        daily_rows=daily_rows,
        monthly_conversions=monthly_conversions,
        has_gsc=has_gsc,
        has_ga4=True,
    )
