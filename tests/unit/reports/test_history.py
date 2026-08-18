"""The Historical Data block (task-45 §2.4): the month window, the grouping,
the `None`-vs-`0` discriminator and the fixed organic attribution.
"""

from __future__ import annotations

from datetime import date

from app.reports.api_source import GscDailyRow
from app.reports.history import (
    build_history,
    compute_ctr,
    fetch_history,
    group_daily_by_month,
    month_keys,
    window_bounds,
)

_GA4_FIELDS = {
    "users": "totalUsers",
    "conversions": "conversions",
    "channel": "firstUserDefaultChannelGroup",
    "month": "yearMonth",
}
_GSC_FIELDS = {"clicks": "clicks", "impressions": "impressions", "date": "date"}


class FakeNinjaClient:
    """Scripted rows keyed by `integration_id`, recording every call so the
    SHAPE of the query (which dimensions were asked for) can be asserted.
    """

    def __init__(self, rows_by_integration: dict[str, list[dict]]):
        self._rows = rows_by_integration
        self.calls: list[tuple] = []

    def query(
        self, integration_id, connection_key, account_id, fields, date_start, date_end,
        *, limit=100, settings=None,
    ):
        self.calls.append((integration_id, tuple(fields), date_start, date_end, limit, settings))
        return self._rows.get(integration_id, [])


# --- the window: complete months only -----------------------------------------


def test_month_keys_stops_at_the_last_complete_month():
    # 17 August 2026 — the still-open August is NOT a history row (task-45 §2.4).
    keys = month_keys(count=12, today=date(2026, 8, 17))
    assert len(keys) == 12
    assert keys[-1] == "2026-07"
    assert keys[0] == "2025-08"
    assert "2026-08" not in keys


def test_month_keys_crosses_the_year_boundary():
    assert month_keys(count=3, today=date(2026, 1, 9)) == ["2025-10", "2025-11", "2025-12"]


def test_window_bounds_spans_whole_months():
    assert window_bounds(["2025-12", "2026-01", "2026-02"]) == (
        date(2025, 12, 1), date(2026, 2, 28)
    )


# --- grouping + CTR --------------------------------------------------------------


def test_group_daily_by_month_sums_days():
    rows = [
        GscDailyRow("2026-07-01", 10, 100),
        GscDailyRow("2026-07-02", 5, 50),
        GscDailyRow("2026-06-30", 1, 9),
    ]
    assert group_daily_by_month(rows) == {"2026-07": (15, 150), "2026-06": (1, 9)}


def test_ctr_matches_reporting_ninjas_own_field():
    # EFFYS July 2026 live: 2 528 / 31 564 = 8.01%, RN's own `ctr` said 8.009%.
    assert round(compute_ctr(2528, 31564) * 100, 2) == 8.01


def test_ctr_is_none_when_impressions_are_zero_or_missing():
    assert compute_ctr(0, 0) is None
    assert compute_ctr(None, 100) is None
    assert compute_ctr(10, None) is None


def test_a_month_without_any_day_row_is_none_not_zero():
    history = build_history(
        keys=["2025-01", "2026-07"],
        daily_rows=[GscDailyRow("2026-07-01", 2528, 31564)],
        monthly_conversions={"2026-07": 654},
    )
    january, july = history.months
    assert (january.clicks, january.impressions, january.ctr) == (None, None, None)
    assert january.organic_conversions is None
    assert (july.clicks, july.impressions, july.organic_conversions) == (2528, 31564, 654)


def test_a_genuinely_zero_day_stays_zero():
    history = build_history(
        keys=["2026-07"],
        daily_rows=[GscDailyRow("2026-07-01", 0, 0)],
        monthly_conversions={},
    )
    month = history.months[0]
    assert month.clicks == 0
    assert month.impressions == 0
    assert month.ctr is None  # 0/0 is undefined, not 0.00%


def test_client_without_gsc_still_gets_the_block_with_conversions():
    history = build_history(
        keys=["2026-07"], daily_rows=[], monthly_conversions={"2026-07": 515}, has_gsc=False
    )
    month = history.months[0]
    assert history.has_gsc is False
    assert (month.clicks, month.impressions, month.ctr) == (None, None, None)
    assert month.organic_conversions == 515


# --- fetch_history: query shape --------------------------------------------------


def test_fetch_history_uses_first_user_channel_grouping_never_session_medium():
    """task-45 §1.5: the ported `historical_fetcher.py` read organic through
    `sessionMedium`, the same wrong attribution task-42 fixed everywhere else.
    Carrying it over would make this block disagree with the `Organic
    Conversions` row of the very same workbook by 5-15%.
    """
    client = FakeNinjaClient(
        {
            "google_search_console": [
                {"date": "2026-07-01", "clicks": 2528, "impressions": 31564}
            ],
            "ga4": [
                {
                    "yearMonth": "202607",
                    "firstUserDefaultChannelGroup": "Organic Search",
                    "conversions": 654,
                },
                {
                    "yearMonth": "202607",
                    "firstUserDefaultChannelGroup": "Organic Social",
                    "conversions": 26,
                },
            ],
        }
    )
    history = fetch_history(
        client,
        ga4_connection_key="seo@x",
        ga4_account_id="properties/1",
        ga4_fields=_GA4_FIELDS,
        gsc_ref=("seo@x", "sc-domain:effys.com"),
        gsc_fields=_GSC_FIELDS,
        months=2,
        today=date(2026, 8, 17),
    )

    ga4_call = next(call for call in client.calls if call[0] == "ga4")
    assert ga4_call[1] == ("yearMonth", "firstUserDefaultChannelGroup", "conversions")
    assert "sessionMedium" not in ga4_call[1]

    july = next(m for m in history.months if m.month == "2026-07")
    assert july.organic_conversions == 654  # not 654 + 26


def test_fetch_history_asks_gsc_for_days_over_the_whole_window_once():
    client = FakeNinjaClient({"google_search_console": [], "ga4": []})
    fetch_history(
        client,
        ga4_connection_key="seo@x",
        ga4_account_id="properties/1",
        ga4_fields=_GA4_FIELDS,
        gsc_ref=("seo@x", "sc-domain:effys.com"),
        gsc_fields=_GSC_FIELDS,
        months=12,
        today=date(2026, 8, 17),
    )
    gsc_calls = [call for call in client.calls if call[0] == "google_search_console"]
    assert len(gsc_calls) == 1
    _integration, fields, date_start, date_end, limit, settings = gsc_calls[0]
    assert fields == ("date", "clicks", "impressions")
    assert (date_start, date_end) == ("2025-08-01", "2026-07-31")
    assert settings == {"search_type": "web"}
    # A year of days must not be cut off by the client's default limit=100.
    assert limit > 365


def test_fetch_history_without_gsc_skips_the_gsc_query_entirely():
    client = FakeNinjaClient({"ga4": []})
    history = fetch_history(
        client,
        ga4_connection_key="seo@x",
        ga4_account_id="properties/1",
        ga4_fields=_GA4_FIELDS,
        gsc_ref=None,
        gsc_fields=None,
        months=3,
        today=date(2026, 8, 17),
    )
    assert [call[0] for call in client.calls] == ["ga4"]
    assert history.has_gsc is False
    assert len(history.months) == 3
