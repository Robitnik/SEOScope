"""Monthly Performance table (task-54): the operator's own Google Sheet,
reproduced from live Reporting Ninja data instead of hand-kept per month.

Two capabilities the hand-kept sheet never had, both forced by live
measurement (task-54 §1):

- **Per-client attribution.** GA4 exposes both a first-user and a
  session-scoped `<...>DefaultChannelGroup` field. The operator's own sheet
  mixed them within a single row without noticing (`Total organic Users`
  matched first-user attribution exactly for 7/7 months in 2026, while
  `Book Now` the very same month matched session attribution exactly) — so
  attribution is a per-client SETTING here, not a hard-coded choice, and the
  default mirrors exactly what the sheet's own latest row did: users from
  first-user, conversions/named-event from session.
- **A named-event column that sums a SET of events, not one.** Several
  clients split one conversion across per-location events (`Book Now 3rd
  Avenue`, `... Hudson`, `... Lincoln Square`, `... Manhattan West` for
  PJC) — picking "the top event" automatically would also have picked
  `cookie_consent_update` for LAXTON, which is consent-banner noise, not a
  conversion.

This module is pure — no DB session, no `ClientMonthlyMetric` — the same
split `app/reports/history.py` already uses: fetch/derive here, persist in
`app/db/repositories/client_monthly.py`.

Two RN traps, both measured live and worth restating because they are easy
to reintroduce by "simplifying" a query:

- Asking for `conversions` AND `keyEvents` in the SAME `/query` throws a
  deterministic `INTERNAL` — they are GA4 aliases of the same metric, and RN
  cannot resolve both in one call. Never combine them.
- GSC's `month` dimension answers a month outside its own data window with a
  LYING zero row (`{clicks: 0, impressions: 0}`), indistinguishable from a
  real zero-traffic month. Only the `date` dimension tells the truth (no
  rows at all) — `api_source.fetch_gsc_daily` already does this; grouped by
  month here, exactly as `history.py` does for its own 12-month block.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Literal

from app.reports.api_source import GscDailyRow, fetch_gsc_daily
from app.reports.history import month_keys, month_label, window_bounds
from app.reports.ninja_api import APIError, RateLimitError, ReportingNinjaClient

GA4_INTEGRATION_ID = "ga4"
GSC_INTEGRATION_ID = "google_search_console"

Attribution = Literal["first_user", "session"]
Ga4State = Literal["ok", "no_data", "failed"]
GscState = Literal["ok", "no_data", "not_configured", "failed"]

_ORGANIC_SEARCH = "organic search"
_GSC_WEB_SETTINGS = {"search_type": "web"}
_OTHER_BUCKET = "(other)"

_MONTH_CANDIDATES = ("yearMonth",)
_USERS_CANDIDATES = ("totalUsers", "activeUsers", "users")
_CONVERSIONS_CANDIDATES = ("conversions", "keyEvents", "goalCompletionsAll")
_EVENT_NAME_CANDIDATES = ("eventName",)
_ATTRIBUTION_CANDIDATES: dict[Attribution, tuple[str, ...]] = {
    "first_user": ("firstUserDefaultChannelGroup",),
    "session": ("sessionDefaultChannelGroup",),
}


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


def _normalize_year_month(value: object) -> str | None:
    """GA4's `yearMonth` is `"202607"`; `"2026-07"` is what this module and
    `history.py` both speak.
    """
    raw = str(value or "").strip()
    if len(raw) == 6 and raw.isdigit():
        return f"{raw[:4]}-{raw[4:]}"
    if len(raw) == 7 and raw[4] == "-" and raw[:4].isdigit() and raw[5:].isdigit():
        return raw
    return None


def discover_ga4_monthly_fields(client: ReportingNinjaClient) -> dict[str, str]:
    """One `/fields` call, everything this module can possibly need from it
    — `discover_ga4_fields` in `api_source.py` stops at first-user
    attribution only, since the 8-metric report it serves never needed
    session attribution or per-event breakdown.
    """
    available = {f["field_id"] for f in client.list_fields(GA4_INTEGRATION_ID)}
    month = _pick(available, _MONTH_CANDIDATES)
    users = _pick(available, _USERS_CANDIDATES)
    conversions = _pick(available, _CONVERSIONS_CANDIDATES)
    if not month or not users or not conversions:
        raise FieldDiscoveryError(
            f"GA4 month/users/conversions field not found. Available: {sorted(available)[:15]}"
        )
    fields = {"month": month, "users": users, "conversions": conversions}
    for attribution, candidates in _ATTRIBUTION_CANDIDATES.items():
        picked = _pick(available, candidates)
        if picked:
            fields[attribution] = picked
    event_name = _pick(available, _EVENT_NAME_CANDIDATES)
    if event_name:
        fields["event_name"] = event_name
    return fields


def _channel_limit(months: int, *, per_month: int = 12, floor: int = 100) -> int:
    """Month x channel-group cardinality is small (~10 channels) — sized
    from the window so widening `client_monthly_history_months` can never
    silently truncate it, same reasoning as `api_source._row_limit`.
    """
    return max(floor, months * per_month + 50)


def _event_limit(months: int, *, per_month: int = 150, floor: int = 300) -> int:
    """The query has NO channel filter (§_sum_named_events filters client-side),
    so cardinality is month x eventName x EVERY channel, not just organic —
    live-measured 74/month on SHB (26 events x 8 channels) and 68/month on
    PJC before this was widened, both of which silently exceeded a
    months*40 budget and set `truncated=True` on every normal refresh
    (found live 2026-08-21). Sized generously, with `truncated` (below) as
    the honest fallback if a client still exceeds even this.
    """
    return max(floor, months * per_month + 50)


def _sum_by_month(
    rows: list[dict[str, Any]],
    *,
    month_field: str,
    channel_field: str,
    metric_field: str,
    target: str = _ORGANIC_SEARCH,
) -> dict[str, int]:
    """`{"2026-07": 721}` — summed over rows whose channel EXACTLY matches
    `target` (case-insensitive), never a substring match: `"Organic Search"`
    must never also absorb `"Organic Social"`/`"Organic Shopping"` (the
    exact bug behind the operator sheet's `111` vs `109` mismatch, task-54
    §1.2 point 4).
    """
    result: dict[str, int] = {}
    for row in rows:
        if str(row.get(channel_field, "")).strip().lower() != target:
            continue
        month = _normalize_year_month(row.get(month_field))
        if month is None:
            continue
        result[month] = result.get(month, 0) + (_int(row.get(metric_field)) or 0)
    return result


def _sum_named_events(
    rows: list[dict[str, Any]],
    *,
    month_field: str,
    event_field: str,
    channel_field: str,
    metric_field: str,
    event_names: list[str],
    target: str = _ORGANIC_SEARCH,
) -> tuple[dict[str, int], bool]:
    """`({"2026-07": 70}, has_other_bucket)` — sums `metric_field` for rows
    whose `event_field` is one of `event_names` (case-insensitive) AND whose
    channel exactly matches `target`. `has_other_bucket` is True when GA4
    folded some low-frequency rows into its own `"(other)"` bucket — a
    signal the true count might be an undercount, surfaced as `truncated`
    (task-54 §2.3) rather than a silently-trustworthy number.
    """
    wanted = {name.strip().lower() for name in event_names}
    result: dict[str, int] = {}
    has_other = False
    for row in rows:
        if str(row.get(channel_field, "")).strip().lower() != target:
            continue
        event_name = str(row.get(event_field, "")).strip()
        if event_name.lower() == _OTHER_BUCKET:
            has_other = True
        if event_name.lower() not in wanted:
            continue
        month = _normalize_year_month(row.get(month_field))
        if month is None:
            continue
        result[month] = result.get(month, 0) + (_int(row.get(metric_field)) or 0)
    return result, has_other


def _group_gsc_daily_by_month(rows: list[GscDailyRow]) -> dict[str, tuple[int, int, int]]:
    """`{"2025-04": (clicks, impressions, days_with_data)}` — the day count
    is the §1.4б partial-month signal (23/30 days at the edge of GSC's
    16-month window), which plain `history.group_daily_by_month` doesn't
    track because its own caller never needed it.
    """
    grouped: dict[str, tuple[int, int, int]] = {}
    for row in rows:
        day = row.date
        if len(day) < 7 or day[4] != "-":
            continue
        key = day[:7]
        clicks, impressions, days = grouped.get(key, (0, 0, 0))
        grouped[key] = (
            clicks + (row.clicks or 0),
            impressions + (row.impressions or 0),
            days + 1,
        )
    return grouped


@dataclass(frozen=True)
class MonthFetch:
    """Raw per-month numbers for exactly the months a single `fetch_months`
    call was asked for — merging this with already-frozen DB rows into a
    full-window table is `app.db.repositories.client_monthly`'s job, not
    this module's (kept DB-free, same split as `history.py`).
    """

    month: str
    organic_users: int | None
    organic_conversions: int | None
    key_event_count: int | None
    gsc_clicks: int | None
    gsc_impressions: int | None
    gsc_days_with_data: int | None
    ga4_state: Ga4State
    gsc_state: GscState
    truncated: bool


@dataclass(frozen=True)
class FetchResult:
    months: list[MonthFetch]
    has_gsc: bool
    has_key_event_column: bool
    requests_used: int
    ga4_error: str | None = None
    gsc_error: str | None = None


def fetch_months(
    client: ReportingNinjaClient,
    *,
    ga4_connection_key: str,
    ga4_account_id: str,
    ga4_fields: dict[str, str],
    gsc_ref: tuple[str, str] | None,
    gsc_fields: dict[str, str] | None,
    keys: list[str],
    attribution_users: Attribution,
    attribution_events: Attribution,
    key_event_names: list[str],
) -> FetchResult:
    """Fetch exactly `keys` (not necessarily the full window — the
    repository narrows this to the un-frozen tail on a plain `/refresh`, see
    task-54 §2.2/§2.3). 3 requests when `attribution_users ==
    attribution_events` (users+conversions share one query), 4 otherwise —
    plus one for GSC. All requests for one provider are wrapped in a single
    try/except: a mid-window RN failure marks that WHOLE provider `failed`
    for every requested month rather than guessing which months are still
    trustworthy (PLAN §12 — one honest failure state beats partial
    inference).
    """
    if not keys:
        return FetchResult(
            months=[],
            has_gsc=gsc_ref is not None,
            has_key_event_column=bool(key_event_names),
            requests_used=0,
        )

    date_from, date_to = window_bounds(sorted(keys))
    requests_before = client.request_count

    month_field = ga4_fields["month"]
    users_field = ga4_fields["users"]
    conversions_field = ga4_fields["conversions"]
    users_channel_field = _attribution_field(ga4_fields, attribution_users)
    events_channel_field = _attribution_field(ga4_fields, attribution_events)

    users_by_month: dict[str, int] = {}
    conv_by_month: dict[str, int] = {}
    ga4_error: str | None = None
    ga4_truncated = False
    try:
        if attribution_users == attribution_events:
            limit = _channel_limit(len(keys))
            rows = client.query(
                GA4_INTEGRATION_ID,
                ga4_connection_key,
                ga4_account_id,
                [month_field, users_channel_field, users_field, conversions_field],
                date_from.isoformat(),
                date_to.isoformat(),
                limit=limit,
            )
            ga4_truncated = len(rows) >= limit
            users_by_month = _sum_by_month(
                rows,
                month_field=month_field,
                channel_field=users_channel_field,
                metric_field=users_field,
            )
            conv_by_month = _sum_by_month(
                rows,
                month_field=month_field,
                channel_field=users_channel_field,
                metric_field=conversions_field,
            )
        else:
            limit = _channel_limit(len(keys))
            users_rows = client.query(
                GA4_INTEGRATION_ID,
                ga4_connection_key,
                ga4_account_id,
                [month_field, users_channel_field, users_field],
                date_from.isoformat(),
                date_to.isoformat(),
                limit=limit,
            )
            ga4_truncated = len(users_rows) >= limit
            users_by_month = _sum_by_month(
                users_rows,
                month_field=month_field,
                channel_field=users_channel_field,
                metric_field=users_field,
            )
            conv_rows = client.query(
                GA4_INTEGRATION_ID,
                ga4_connection_key,
                ga4_account_id,
                [month_field, events_channel_field, conversions_field],
                date_from.isoformat(),
                date_to.isoformat(),
                limit=limit,
            )
            ga4_truncated = ga4_truncated or len(conv_rows) >= limit
            conv_by_month = _sum_by_month(
                conv_rows,
                month_field=month_field,
                channel_field=events_channel_field,
                metric_field=conversions_field,
            )
    except (APIError, RateLimitError) as exc:
        ga4_error = str(exc)

    events_by_month: dict[str, int] = {}
    has_key_event_column = bool(key_event_names)
    if has_key_event_column and ga4_error is None:
        event_name_field = ga4_fields.get("event_name")
        if event_name_field is None:
            ga4_error = "GA4 eventName field not available on this account"
        else:
            try:
                limit = _event_limit(len(keys))
                event_rows = client.query(
                    GA4_INTEGRATION_ID,
                    ga4_connection_key,
                    ga4_account_id,
                    [month_field, event_name_field, events_channel_field, conversions_field],
                    date_from.isoformat(),
                    date_to.isoformat(),
                    limit=limit,
                )
                events_by_month, has_other = _sum_named_events(
                    event_rows,
                    month_field=month_field,
                    event_field=event_name_field,
                    channel_field=events_channel_field,
                    metric_field=conversions_field,
                    event_names=key_event_names,
                )
                ga4_truncated = ga4_truncated or len(event_rows) >= limit or has_other
            except (APIError, RateLimitError) as exc:
                ga4_error = str(exc)

    gsc_by_month: dict[str, tuple[int, int, int]] = {}
    gsc_error: str | None = None
    has_gsc = gsc_ref is not None and gsc_fields is not None and "date" in (gsc_fields or {})
    if has_gsc and gsc_ref is not None and gsc_fields is not None:
        gsc_connection_key, gsc_account_id = gsc_ref
        try:
            daily = fetch_gsc_daily(
                client, gsc_connection_key, gsc_account_id, date_from, date_to, gsc_fields
            )
            gsc_by_month = _group_gsc_daily_by_month(daily)
        except (APIError, RateLimitError) as exc:
            gsc_error = str(exc)

    months: list[MonthFetch] = []
    for key in keys:
        ga4_state: Ga4State = (
            "failed"
            if ga4_error
            else ("ok" if key in users_by_month or key in conv_by_month else "no_data")
        )
        gsc_state: GscState
        if gsc_ref is None:
            gsc_state = "not_configured"
        elif gsc_error:
            gsc_state = "failed"
        else:
            gsc_state = "ok" if key in gsc_by_month else "no_data"
        clicks, impressions, days_with_data = gsc_by_month.get(key, (None, None, None))
        months.append(
            MonthFetch(
                month=key,
                organic_users=None if ga4_error else users_by_month.get(key),
                organic_conversions=None if ga4_error else conv_by_month.get(key),
                key_event_count=(
                    None if (ga4_error or not has_key_event_column) else events_by_month.get(key)
                ),
                gsc_clicks=None if gsc_error else clicks,
                gsc_impressions=None if gsc_error else impressions,
                gsc_days_with_data=None if gsc_error else days_with_data,
                ga4_state=ga4_state,
                gsc_state=gsc_state,
                truncated=ga4_truncated,
            )
        )

    requests_used = client.request_count - requests_before
    return FetchResult(
        months=months,
        has_gsc=has_gsc,
        has_key_event_column=has_key_event_column,
        requests_used=requests_used,
        ga4_error=ga4_error,
        gsc_error=gsc_error,
    )


def _attribution_field(ga4_fields: dict[str, str], attribution: Attribution) -> str:
    field_id = ga4_fields.get(attribution)
    if field_id is None:
        raise FieldDiscoveryError(
            f"GA4 {attribution} attribution field not available on this account"
        )
    return field_id


def days_in_month(key: str) -> int:
    year, month = (int(part) for part in key.split("-"))
    return calendar.monthrange(year, month)[1]


Direction = Literal["up", "down", "flat"]
_DIRECTION_KEYS = (
    "organic_users",
    "gsc_impressions",
    "gsc_clicks",
    "organic_conversions",
    "conversion_rate",
    "key_event_count",
    "ctr",
)


@dataclass(frozen=True)
class MonthlyRow:
    month: str
    label: str
    days_in_month: int
    organic_users: int | None
    organic_conversions: int | None
    key_event_count: int | None
    gsc_clicks: int | None
    gsc_impressions: int | None
    gsc_days_with_data: int | None
    conversion_rate: float | None
    ctr: float | None
    is_partial_period: bool
    ga4_state: Ga4State
    gsc_state: GscState
    truncated: bool
    directions: dict[str, Direction | None] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "month": self.month,
            "label": self.label,
            "days_in_month": self.days_in_month,
            "organic_users": self.organic_users,
            "organic_conversions": self.organic_conversions,
            "key_event_count": self.key_event_count,
            "gsc_clicks": self.gsc_clicks,
            "gsc_impressions": self.gsc_impressions,
            "gsc_days_with_data": self.gsc_days_with_data,
            "conversion_rate": self.conversion_rate,
            "ctr": self.ctr,
            "is_partial_period": self.is_partial_period,
            "ga4_state": self.ga4_state,
            "gsc_state": self.gsc_state,
            "truncated": self.truncated,
            "directions": self.directions,
        }


def safe_ratio(numerator: int | None, denominator: int | None) -> float | None:
    """`numerator / denominator`, or `None` when it isn't a number: a
    missing side, or a zero denominator (dividing by which is undefined,
    not `0.00%` — PLAN §12, same rule `history.compute_ctr` already uses for
    CTR specifically; this is the shared form for both `CTR` and
    `Conversion rate`).
    """
    if numerator is None or denominator is None or denominator == 0:
        return None
    return numerator / denominator


def build_rows(fetched: list[MonthFetch], days_in_month_by_key: dict[str, int]) -> list[MonthlyRow]:
    """Derive `conversion_rate`/`ctr`/`is_partial_period`/`directions` for a
    set of already-fetched (or DB-loaded) months. Oldest-first input,
    directions computed against the PREVIOUS row in the same list — so the
    caller must pass the full window (frozen rows included), not just the
    freshly-fetched tail, or the first fresh month would show no direction.
    """
    rows: list[MonthlyRow] = []
    previous: MonthlyRow | None = None
    for item in fetched:
        days_count = days_in_month_by_key.get(item.month) or days_in_month(item.month)
        is_partial = (
            item.gsc_days_with_data is not None and 0 < item.gsc_days_with_data < days_count
        )
        conversion_rate = safe_ratio(item.organic_conversions, item.organic_users)
        ctr = safe_ratio(item.gsc_clicks, item.gsc_impressions)
        row = MonthlyRow(
            month=item.month,
            label=month_label(item.month),
            days_in_month=days_count,
            organic_users=item.organic_users,
            organic_conversions=item.organic_conversions,
            key_event_count=item.key_event_count,
            gsc_clicks=item.gsc_clicks,
            gsc_impressions=item.gsc_impressions,
            gsc_days_with_data=item.gsc_days_with_data,
            conversion_rate=conversion_rate,
            ctr=ctr,
            is_partial_period=is_partial,
            ga4_state=item.ga4_state,
            gsc_state=item.gsc_state,
            truncated=item.truncated,
            directions=_directions(item, conversion_rate, ctr, previous),
        )
        rows.append(row)
        previous = row
    return rows


def _directions(
    item: MonthFetch,
    conversion_rate: float | None,
    ctr: float | None,
    previous: MonthlyRow | None,
) -> dict[str, Direction | None]:
    if previous is None:
        return dict.fromkeys(_DIRECTION_KEYS)
    current_values: dict[str, float | int | None] = {
        "organic_users": item.organic_users,
        "gsc_impressions": item.gsc_impressions,
        "gsc_clicks": item.gsc_clicks,
        "organic_conversions": item.organic_conversions,
        "conversion_rate": conversion_rate,
        "key_event_count": item.key_event_count,
        "ctr": ctr,
    }
    previous_values: dict[str, float | int | None] = {
        "organic_users": previous.organic_users,
        "gsc_impressions": previous.gsc_impressions,
        "gsc_clicks": previous.gsc_clicks,
        "organic_conversions": previous.organic_conversions,
        "conversion_rate": previous.conversion_rate,
        "key_event_count": previous.key_event_count,
        "ctr": previous.ctr,
    }
    return {key: _direction(current_values[key], previous_values[key]) for key in _DIRECTION_KEYS}


def _direction(current: float | int | None, previous: float | int | None) -> Direction | None:
    """All 7 metrics on this table are "higher is better" — no
    `better_direction` lookup needed, unlike the dashboard's general metric
    catalog (PLAN §12 still applies: `None` on either side is `None`, never
    a guessed direction).
    """
    if current is None or previous is None:
        return None
    if current > previous:
        return "up"
    if current < previous:
        return "down"
    return "flat"


def discover_key_events(
    client: ReportingNinjaClient,
    *,
    ga4_connection_key: str,
    ga4_account_id: str,
    ga4_fields: dict[str, str],
    attribution_events: Attribution,
    months: int = 12,
    today: date | None = None,
) -> list[dict[str, Any]]:
    """`[{"event_name": "Book Now", "key_events": 704, "months_seen": 7}]`,
    sorted by `key_events` descending — a menu for the operator's settings
    picker, NOT an auto-selection (task-54 §1.5: the top event for LAXTON is
    `cookie_consent_update`, consent-banner noise, not a conversion).
    """
    event_name_field = ga4_fields.get("event_name")
    if event_name_field is None:
        return []
    channel_field = _attribution_field(ga4_fields, attribution_events)
    keys = month_keys(count=months, today=today)
    if not keys:
        return []
    date_from, date_to = window_bounds(keys)
    rows = client.query(
        GA4_INTEGRATION_ID,
        ga4_connection_key,
        ga4_account_id,
        [ga4_fields["month"], event_name_field, channel_field, ga4_fields["conversions"]],
        date_from.isoformat(),
        date_to.isoformat(),
        limit=_event_limit(len(keys)),
    )
    totals: dict[str, int] = {}
    months_seen: dict[str, set[str]] = {}
    for row in rows:
        if str(row.get(channel_field, "")).strip().lower() != _ORGANIC_SEARCH:
            continue
        event_name = str(row.get(event_name_field, "")).strip()
        if not event_name or event_name.lower() == _OTHER_BUCKET:
            continue
        month = _normalize_year_month(row.get(ga4_fields["month"]))
        value = _int(row.get(ga4_fields["conversions"])) or 0
        if value <= 0:
            continue
        totals[event_name] = totals.get(event_name, 0) + value
        months_seen.setdefault(event_name, set()).add(month or "")
    return [
        {"event_name": name, "key_events": totals[name], "months_seen": len(months_seen[name])}
        for name in sorted(totals, key=lambda n: -totals[n])
    ]


def is_month_settled(month: str, *, today: date, settle_days: int = 3) -> bool:
    """A closed month is "settled" `settle_days` after it ends — GA4 keeps
    revising processed data for roughly 48h after a day closes (task-54
    §2.2), so a month collected too soon after closing must stay eligible
    for re-collection even though it is no longer the OPEN month.
    """
    year, month_num = (int(part) for part in month.split("-"))
    last_day = date(year, month_num, calendar.monthrange(year, month_num)[1])
    if last_day >= today:
        return False
    return (today - last_day).days >= settle_days


def is_row_freezable(month: str, ga4_state: Ga4State, gsc_state: GscState, *, today: date) -> bool:
    """The freeze rule (task-54 §2.2), all three conditions together: closed,
    settled, and the last collection attempt succeeded. `failed` never
    freezes — a month that could not be collected must stay eligible for the
    next `/refresh`, not get stuck showing stale `None`s forever.
    """
    if ga4_state == "failed" or gsc_state == "failed":
        return False
    return is_month_settled(month, today=today)
