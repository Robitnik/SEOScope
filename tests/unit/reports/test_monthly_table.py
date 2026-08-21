"""Monthly Performance table (task-54): attribution, the named-event set,
the two live-measured Reporting Ninja traps (§1.4), and the freeze rule.
"""

from __future__ import annotations

from datetime import date

from app.reports.api_source import GscDailyRow
from app.reports.monthly_table import (
    FieldDiscoveryError,
    MonthFetch,
    build_rows,
    days_in_month,
    discover_ga4_monthly_fields,
    discover_key_events,
    fetch_months,
    is_month_settled,
    is_row_freezable,
    safe_ratio,
)

_GA4_FIELDS = {
    "month": "yearMonth",
    "users": "totalUsers",
    "conversions": "conversions",
    "first_user": "firstUserDefaultChannelGroup",
    "session": "sessionDefaultChannelGroup",
    "event_name": "eventName",
}
_GSC_FIELDS = {"clicks": "clicks", "impressions": "impressions", "date": "date"}
_GSC_REF = ("seo@x", "sc-domain:example.com")


class FakeNinjaClient:
    """Scripted rows keyed by the exact field tuple requested — lets a test
    script DIFFERENT answers for the "combined" vs "separate" query shapes
    that single-attribution vs mixed-attribution mode issue (task-54 §2.3),
    same pattern as `test_history.py::FakeNinjaClient`.
    """

    def __init__(
        self,
        responses: dict[tuple[str, ...], list[dict]] | None = None,
        fields: dict[str, list[dict]] | None = None,
        *,
        raise_on: tuple[str, ...] | None = None,
    ):
        self._responses = responses or {}
        self._fields = fields or {}
        self._raise_on = raise_on
        self.calls: list[tuple] = []
        self.request_count = 0

    def list_fields(self, integration_id: str) -> list[dict]:
        return self._fields.get(integration_id, [])

    def query(
        self,
        integration_id,
        connection_key,
        account_id,
        fields,
        date_start,
        date_end,
        *,
        limit=100,
        settings=None,
    ):
        self.request_count += 1
        key = tuple(fields)
        self.calls.append((integration_id, key, date_start, date_end, limit, settings))
        if self._raise_on is not None and key == self._raise_on:
            from app.reports.ninja_api import APIError

            raise APIError("INTERNAL", "boom")
        return self._responses.get(key, [])


# --- exact channel matching (task-54 §1.2 point 4 / §2.3) -------------------


def test_organic_search_never_absorbs_organic_social():
    rows = [
        {"yearMonth": "202607", "sessionDefaultChannelGroup": "Organic Search", "totalUsers": 109},
        {"yearMonth": "202607", "sessionDefaultChannelGroup": "Organic Social", "totalUsers": 2},
    ]
    client = FakeNinjaClient(
        {("yearMonth", "sessionDefaultChannelGroup", "totalUsers", "conversions"): rows}
    )
    result = fetch_months(
        client,
        ga4_connection_key="k",
        ga4_account_id="a",
        ga4_fields=_GA4_FIELDS,
        gsc_ref=None,
        gsc_fields=None,
        keys=["2026-07"],
        attribution_users="session",
        attribution_events="session",
        key_event_names=[],
    )
    assert result.months[0].organic_users == 109


# --- named-event column sums a SET, not one event (task-54 §1.5) -----------


def test_named_event_column_sums_multiple_locations():
    # PJC-shaped: the same conversion split across per-location events.
    rows = [
        {
            "yearMonth": "202607",
            "eventName": "Book Now 3rd Avenue",
            "sessionDefaultChannelGroup": "Organic Search",
            "conversions": 100,
        },
        {
            "yearMonth": "202607",
            "eventName": "Book Now Hudson",
            "sessionDefaultChannelGroup": "Organic Search",
            "conversions": 50,
        },
        {
            "yearMonth": "202607",
            "eventName": "Phone",
            "sessionDefaultChannelGroup": "Organic Search",
            "conversions": 5,
        },
    ]
    client = FakeNinjaClient(
        {
            ("yearMonth", "sessionDefaultChannelGroup", "totalUsers"): [],
            ("yearMonth", "sessionDefaultChannelGroup", "conversions"): [],
            ("yearMonth", "eventName", "sessionDefaultChannelGroup", "conversions"): rows,
        }
    )
    result = fetch_months(
        client,
        ga4_connection_key="k",
        ga4_account_id="a",
        ga4_fields=_GA4_FIELDS,
        gsc_ref=None,
        gsc_fields=None,
        keys=["2026-07"],
        attribution_users="first_user",
        attribution_events="session",
        key_event_names=["Book Now 3rd Avenue", "Book Now Hudson"],
    )
    assert result.months[0].key_event_count == 150


def test_other_bucket_marks_truncated():
    rows = [
        {
            "yearMonth": "202607",
            "eventName": "Book Now",
            "sessionDefaultChannelGroup": "Organic Search",
            "conversions": 10,
        },
        {
            "yearMonth": "202607",
            "eventName": "(other)",
            "sessionDefaultChannelGroup": "Organic Search",
            "conversions": 3,
        },
    ]
    client = FakeNinjaClient(
        {
            ("yearMonth", "sessionDefaultChannelGroup", "totalUsers"): [],
            ("yearMonth", "sessionDefaultChannelGroup", "conversions"): [],
            ("yearMonth", "eventName", "sessionDefaultChannelGroup", "conversions"): rows,
        }
    )
    result = fetch_months(
        client,
        ga4_connection_key="k",
        ga4_account_id="a",
        ga4_fields=_GA4_FIELDS,
        gsc_ref=None,
        gsc_fields=None,
        keys=["2026-07"],
        attribution_users="first_user",
        attribution_events="session",
        key_event_names=["Book Now"],
    )
    assert result.months[0].key_event_count == 10
    assert result.months[0].truncated is True


# --- the two live-measured RN traps (task-54 §1.4) --------------------------


def test_single_attribution_combines_users_and_conversions_in_one_query():
    # §1.4а: `conversions` and `keyEvents` in ONE query throws INTERNAL — but
    # combining totalUsers+conversions (not two conversion aliases) is safe
    # and measured live to work, so single-attribution mode uses it to save
    # a request (task-54 §2.3 "3 запити").
    rows = [
        {
            "yearMonth": "202607",
            "firstUserDefaultChannelGroup": "Organic Search",
            "totalUsers": 721,
            "conversions": 132,
        }
    ]
    client = FakeNinjaClient(
        {("yearMonth", "firstUserDefaultChannelGroup", "totalUsers", "conversions"): rows}
    )
    result = fetch_months(
        client,
        ga4_connection_key="k",
        ga4_account_id="a",
        ga4_fields=_GA4_FIELDS,
        gsc_ref=None,
        gsc_fields=None,
        keys=["2026-07"],
        attribution_users="first_user",
        attribution_events="first_user",
        key_event_names=[],
    )
    assert result.months[0].organic_users == 721
    assert result.months[0].organic_conversions == 132
    # Exactly one GA4 query for the combined users+conversions fetch — no
    # separate `conversions`-alone or `users`-alone call.
    ga4_calls = [c for c in client.calls if c[0] == "ga4"]
    assert len(ga4_calls) == 1


def test_mixed_attribution_issues_four_requests_users_conv_events_gsc():
    client = FakeNinjaClient(
        {
            ("yearMonth", "firstUserDefaultChannelGroup", "totalUsers"): [
                {
                    "yearMonth": "202607",
                    "firstUserDefaultChannelGroup": "Organic Search",
                    "totalUsers": 721,
                }
            ],
            ("yearMonth", "sessionDefaultChannelGroup", "conversions"): [
                {
                    "yearMonth": "202607",
                    "sessionDefaultChannelGroup": "Organic Search",
                    "conversions": 109,
                }
            ],
            ("yearMonth", "eventName", "sessionDefaultChannelGroup", "conversions"): [
                {
                    "yearMonth": "202607",
                    "eventName": "Book Now",
                    "sessionDefaultChannelGroup": "Organic Search",
                    "conversions": 70,
                }
            ],
            ("date", "clicks", "impressions"): [
                {"date": "2026-07-15", "clicks": 717, "impressions": 69176}
            ],
        }
    )
    result = fetch_months(
        client,
        ga4_connection_key="k",
        ga4_account_id="a",
        ga4_fields=_GA4_FIELDS,
        gsc_ref=_GSC_REF,
        gsc_fields=_GSC_FIELDS,
        keys=["2026-07"],
        attribution_users="first_user",
        attribution_events="session",
        key_event_names=["Book Now"],
    )
    row = result.months[0]
    assert (row.organic_users, row.organic_conversions, row.key_event_count) == (721, 109, 70)
    assert result.requests_used == 4


def test_gsc_month_dimension_never_used_only_date():
    # §1.4б: `month`-dimensioned GSC lies with a zero row for a month
    # outside its own window; this module must only ever ask GSC with
    # `date` (grouped locally), never `month`.
    client = FakeNinjaClient(
        {("yearMonth", "firstUserDefaultChannelGroup", "totalUsers", "conversions"): []}
    )
    fetch_months(
        client,
        ga4_connection_key="k",
        ga4_account_id="a",
        ga4_fields=_GA4_FIELDS,
        gsc_ref=_GSC_REF,
        gsc_fields=_GSC_FIELDS,
        keys=["2026-07"],
        attribution_users="first_user",
        attribution_events="first_user",
        key_event_names=[],
    )
    gsc_calls = [c for c in client.calls if c[0] == "google_search_console"]
    assert len(gsc_calls) == 1
    assert gsc_calls[0][1] == ("date", "clicks", "impressions")


def test_gsc_absent_month_is_none_not_zero():
    # A month with zero GSC day-rows (outside the 16-month window) must read
    # `no_data`/`None`, never a lying zero (task-54 §1.4б).
    client = FakeNinjaClient(
        {("yearMonth", "firstUserDefaultChannelGroup", "totalUsers", "conversions"): []}
    )
    result = fetch_months(
        client,
        ga4_connection_key="k",
        ga4_account_id="a",
        ga4_fields=_GA4_FIELDS,
        gsc_ref=_GSC_REF,
        gsc_fields=_GSC_FIELDS,
        keys=["2024-12"],
        attribution_users="first_user",
        attribution_events="first_user",
        key_event_names=[],
    )
    row = result.months[0]
    assert row.gsc_clicks is None
    assert row.gsc_impressions is None
    assert row.gsc_state == "no_data"


def test_partial_month_at_the_edge_of_the_gsc_window():
    # SDS-shaped: Apr 2025 has 23 of 30 days at the edge of the 16-month
    # window (task-54 §1.4б).
    daily = [GscDailyRow(f"2025-04-{d:02d}", 10, 100) for d in range(1, 24)]
    row = build_rows(
        [
            MonthFetch(
                month="2025-04",
                organic_users=740,
                organic_conversions=199,
                key_event_count=None,
                gsc_clicks=sum(r.clicks for r in daily),
                gsc_impressions=sum(r.impressions for r in daily),
                gsc_days_with_data=len(daily),
                ga4_state="ok",
                gsc_state="ok",
                truncated=False,
            )
        ],
        {"2025-04": 30},
    )[0]
    assert row.is_partial_period is True
    assert row.gsc_days_with_data == 23


def test_full_month_is_not_partial():
    row = build_rows(
        [
            MonthFetch(
                month="2026-07",
                organic_users=721,
                organic_conversions=109,
                key_event_count=70,
                gsc_clicks=717,
                gsc_impressions=69176,
                gsc_days_with_data=31,
                ga4_state="ok",
                gsc_state="ok",
                truncated=False,
            )
        ],
        {"2026-07": 31},
    )[0]
    assert row.is_partial_period is False


# --- derived metrics: None ≠ 0 (PLAN §12) -----------------------------------


def test_safe_ratio_none_on_missing_side_or_zero_denominator():
    assert safe_ratio(109, 721) == 109 / 721
    assert safe_ratio(None, 721) is None
    assert safe_ratio(109, None) is None
    assert safe_ratio(109, 0) is None
    assert safe_ratio(0, 100) == 0.0  # a genuine zero conversion rate is 0, not None


def test_conversion_rate_and_ctr_are_computed_not_fetched():
    row = build_rows(
        [
            MonthFetch(
                month="2026-07",
                organic_users=721,
                organic_conversions=109,
                key_event_count=70,
                gsc_clicks=717,
                gsc_impressions=69176,
                gsc_days_with_data=31,
                ga4_state="ok",
                gsc_state="ok",
                truncated=False,
            )
        ],
        {"2026-07": 31},
    )[0]
    assert round(row.conversion_rate * 100, 2) == round(109 / 721 * 100, 2)
    assert round(row.ctr * 100, 2) == 1.04


# --- MoM direction: "higher is better" for all 7 metrics --------------------


def test_direction_is_none_for_the_first_row():
    row = build_rows(
        [MonthFetch("2026-06", 100, 10, 5, 50, 500, 30, "ok", "ok", False)], {"2026-06": 30}
    )[0]
    assert all(v is None for v in row.directions.values())


def test_direction_up_down_flat():
    rows = build_rows(
        [
            MonthFetch("2026-06", 100, 10, 5, 50, 500, 30, "ok", "ok", False),
            MonthFetch("2026-07", 120, 8, 5, 60, 400, 31, "ok", "ok", False),
        ],
        {"2026-06": 30, "2026-07": 31},
    )
    directions = rows[1].directions
    assert directions["organic_users"] == "up"  # 120 > 100
    assert directions["organic_conversions"] == "down"  # 8 < 10
    assert directions["key_event_count"] == "flat"  # 5 == 5
    assert directions["gsc_impressions"] == "down"  # 400 < 500


def test_direction_is_none_when_either_side_is_missing():
    rows = build_rows(
        [
            MonthFetch("2026-06", None, None, None, None, None, None, "no_data", "no_data", False),
            MonthFetch("2026-07", 120, 8, 5, 60, 400, 31, "ok", "ok", False),
        ],
        {"2026-06": 30, "2026-07": 31},
    )
    assert all(v is None for v in rows[1].directions.values())


# --- the freeze rule (task-54 §2.2): closed + settled + not failed ---------


def test_open_month_is_never_settled():
    assert is_month_settled("2026-08", today=date(2026, 8, 21)) is False


def test_closed_month_needs_three_days_to_settle():
    assert is_month_settled("2026-07", today=date(2026, 8, 1)) is False  # 1 day
    assert is_month_settled("2026-07", today=date(2026, 8, 2)) is False  # 2 days
    assert is_month_settled("2026-07", today=date(2026, 8, 3)) is True  # 3 days


def test_failed_state_never_freezes_even_if_settled():
    assert is_row_freezable("2026-01", "failed", "ok", today=date(2026, 8, 21)) is False
    assert is_row_freezable("2026-01", "ok", "failed", today=date(2026, 8, 21)) is False


def test_no_data_and_not_configured_do_freeze():
    # A month permanently outside GSC's window is not a fetch FAILURE — it
    # must freeze so it is never re-queried (task-54 §2.2).
    assert is_row_freezable("2024-12", "ok", "no_data", today=date(2026, 8, 21)) is True
    assert is_row_freezable("2024-12", "ok", "not_configured", today=date(2026, 8, 21)) is True


def test_unsettled_month_does_not_freeze_even_if_successful():
    assert is_row_freezable("2026-08", "ok", "ok", today=date(2026, 8, 21)) is False


# --- provider failure is one honest state, not per-cell guessing -----------


def test_ga4_failure_marks_the_whole_window_failed():
    client = FakeNinjaClient(
        raise_on=("yearMonth", "firstUserDefaultChannelGroup", "totalUsers", "conversions")
    )
    result = fetch_months(
        client,
        ga4_connection_key="k",
        ga4_account_id="a",
        ga4_fields=_GA4_FIELDS,
        gsc_ref=None,
        gsc_fields=None,
        keys=["2026-06", "2026-07"],
        attribution_users="first_user",
        attribution_events="first_user",
        key_event_names=[],
    )
    assert all(m.ga4_state == "failed" for m in result.months)
    assert all(m.organic_users is None for m in result.months)
    assert result.ga4_error is not None


def test_gsc_failure_does_not_block_a_successful_ga4_fetch():
    client = FakeNinjaClient(
        {
            ("yearMonth", "firstUserDefaultChannelGroup", "totalUsers", "conversions"): [
                {
                    "yearMonth": "202607",
                    "firstUserDefaultChannelGroup": "Organic Search",
                    "totalUsers": 721,
                    "conversions": 132,
                }
            ],
        },
        raise_on=("date", "clicks", "impressions"),
    )
    result = fetch_months(
        client,
        ga4_connection_key="k",
        ga4_account_id="a",
        ga4_fields=_GA4_FIELDS,
        gsc_ref=_GSC_REF,
        gsc_fields=_GSC_FIELDS,
        keys=["2026-07"],
        attribution_users="first_user",
        attribution_events="first_user",
        key_event_names=[],
    )
    row = result.months[0]
    assert row.organic_users == 721
    assert row.ga4_state == "ok"
    assert row.gsc_state == "failed"


def test_no_gsc_integration_is_not_configured_not_a_failure():
    client = FakeNinjaClient(
        {("yearMonth", "firstUserDefaultChannelGroup", "totalUsers", "conversions"): []}
    )
    result = fetch_months(
        client,
        ga4_connection_key="k",
        ga4_account_id="a",
        ga4_fields=_GA4_FIELDS,
        gsc_ref=None,
        gsc_fields=None,
        keys=["2026-07"],
        attribution_users="first_user",
        attribution_events="first_user",
        key_event_names=[],
    )
    assert result.months[0].gsc_state == "not_configured"
    assert result.has_gsc is False
    assert not [c for c in client.calls if c[0] == "google_search_console"]


# --- field discovery -----------------------------------------------------


def test_discover_ga4_monthly_fields_raises_without_required_fields():
    client = FakeNinjaClient(fields={"ga4": [{"field_id": "totalUsers"}]})
    try:
        discover_ga4_monthly_fields(client)
    except FieldDiscoveryError:
        pass
    else:
        raise AssertionError("expected FieldDiscoveryError")


def test_discover_ga4_monthly_fields_picks_both_attributions_and_event_name():
    client = FakeNinjaClient(
        fields={
            "ga4": [
                {"field_id": "yearMonth"},
                {"field_id": "totalUsers"},
                {"field_id": "conversions"},
                {"field_id": "firstUserDefaultChannelGroup"},
                {"field_id": "sessionDefaultChannelGroup"},
                {"field_id": "eventName"},
            ]
        }
    )
    fields = discover_ga4_monthly_fields(client)
    assert fields["first_user"] == "firstUserDefaultChannelGroup"
    assert fields["session"] == "sessionDefaultChannelGroup"
    assert fields["event_name"] == "eventName"


# --- key-event discovery: a MENU, never an auto-pick (task-54 §1.5) --------


def test_discover_key_events_excludes_other_bucket_and_sorts_by_volume():
    rows = [
        {
            "yearMonth": "202607",
            "eventName": "Book Now",
            "sessionDefaultChannelGroup": "Organic Search",
            "conversions": 70,
        },
        {
            "yearMonth": "202607",
            "eventName": "cookie_consent_update",
            "sessionDefaultChannelGroup": "Organic Search",
            "conversions": 993,
        },
        {
            "yearMonth": "202607",
            "eventName": "(other)",
            "sessionDefaultChannelGroup": "Organic Search",
            "conversions": 5,
        },
        {
            "yearMonth": "202607",
            "eventName": "Phone",
            "sessionDefaultChannelGroup": "Direct",
            "conversions": 200,
        },
    ]
    client = FakeNinjaClient(
        {("yearMonth", "eventName", "sessionDefaultChannelGroup", "conversions"): rows}
    )
    candidates = discover_key_events(
        client,
        ga4_connection_key="k",
        ga4_account_id="a",
        ga4_fields=_GA4_FIELDS,
        attribution_events="session",
        months=1,
        today=date(2026, 8, 21),
    )
    names = [c["event_name"] for c in candidates]
    # LAXTON-shaped: the top event by volume is consent-banner noise — the
    # module surfaces it (it does not pretend it doesn't exist), it is the
    # CALLER's job (the operator, via settings) not to auto-pick it.
    assert names[0] == "cookie_consent_update"
    assert "(other)" not in names
    assert "Phone" not in names  # different channel, excluded


def test_days_in_month_matches_calendar():
    assert days_in_month("2026-07") == 31
    assert days_in_month("2024-02") == 29  # leap year
