from __future__ import annotations

from datetime import date

import pytest

from app.reports.api_source import (
    FieldDiscoveryError,
    discover_ga4_fields,
    discover_gsc_fields,
    fetch_ga4_period,
    fetch_gsc_daily,
    fetch_gsc_period,
    fetch_period_metrics,
)

_DATE_FROM, _DATE_TO = date(2026, 7, 1), date(2026, 7, 31)


class FakeNinjaClient:
    """Duck-types `ReportingNinjaClient`'s query surface with scripted rows."""

    def __init__(self, fields: dict[str, list[dict[str, object]]], queries: dict[str, list[dict]]):
        self._fields = fields
        self._queries = queries
        self.calls: list[tuple] = []
        self.limits: list[int] = []

    def list_fields(self, integration_id: str) -> list[dict[str, object]]:
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
        self.calls.append((integration_id, tuple(fields), settings))
        self.limits.append(limit)
        key = f"{integration_id}:{len(fields)}"
        return self._queries.get(key, [])


_GA4_FIELDS = [
    {"field_id": "totalUsers"},
    {"field_id": "conversions"},
    {"field_id": "firstUserDefaultChannelGroup"},
]
_GSC_FIELDS = [{"field_id": "clicks"}, {"field_id": "impressions"}, {"field_id": "date"}]
_GSC_FIELD_IDS_WITH_DATE = {"clicks": "clicks", "impressions": "impressions", "date": "date"}
_GA4_FIELD_IDS_WITH_CHANNEL = {
    "users": "totalUsers",
    "conversions": "conversions",
    "channel": "firstUserDefaultChannelGroup",
}


def test_discover_ga4_fields_picks_the_channel_dimension():
    client = FakeNinjaClient({"ga4": _GA4_FIELDS}, {})
    fields = discover_ga4_fields(client)
    assert fields == {
        "users": "totalUsers",
        "conversions": "conversions",
        "channel": "firstUserDefaultChannelGroup",
    }


def test_discover_ga4_fields_raises_when_users_metric_is_absent():
    client = FakeNinjaClient({"ga4": [{"field_id": "conversions"}]}, {})
    with pytest.raises(FieldDiscoveryError):
        discover_ga4_fields(client)


def test_discover_gsc_fields_raises_when_impressions_is_absent():
    client = FakeNinjaClient({"google_search_console": [{"field_id": "clicks"}]}, {})
    with pytest.raises(FieldDiscoveryError):
        discover_gsc_fields(client)


def test_fetch_ga4_period_matches_the_effys_live_measurement_exactly():
    # task-42 §1.3 EFFYS July 2026 live numbers.
    client = FakeNinjaClient(
        {},
        {
            "ga4:2": [{"totalUsers": "6251", "conversions": "1099"}],
            "ga4:3": [
                {
                    "firstUserDefaultChannelGroup": "Organic Search",
                    "totalUsers": 3061,
                    "conversions": 654,
                },
                {"firstUserDefaultChannelGroup": "Direct", "totalUsers": 2858, "conversions": 391},
                {
                    "firstUserDefaultChannelGroup": "Organic Social",
                    "totalUsers": 137,
                    "conversions": 26,
                },
            ],
        },
    )
    result = fetch_ga4_period(
        client, "seo@x", "properties/1", _DATE_FROM, _DATE_TO, _GA4_FIELD_IDS_WITH_CHANNEL
    )
    assert result == {
        "users": 6251,
        "organic_users": 3061,
        "direct_users": 2858,
        "conversions": 1099,
        "organic_conversions": 654,
        "direct_conversions": 391,
    }


def test_organic_search_never_absorbs_organic_social_or_shopping():
    client = FakeNinjaClient(
        {},
        {
            "ga4:2": [{"totalUsers": "200", "conversions": "0"}],
            "ga4:3": [
                {
                    "firstUserDefaultChannelGroup": "Organic Search",
                    "totalUsers": 100,
                    "conversions": 0,
                },
                {
                    "firstUserDefaultChannelGroup": "Organic Social",
                    "totalUsers": 50,
                    "conversions": 0,
                },
                {
                    "firstUserDefaultChannelGroup": "Organic Shopping",
                    "totalUsers": 50,
                    "conversions": 0,
                },
            ],
        },
    )
    result = fetch_ga4_period(
        client, "seo@x", "properties/1", _DATE_FROM, _DATE_TO, _GA4_FIELD_IDS_WITH_CHANNEL
    )
    assert result["organic_users"] == 100  # not 100+50+50


def test_a_genuine_zero_total_is_not_turned_into_none():
    client = FakeNinjaClient({}, {"ga4:2": [{"totalUsers": 0, "conversions": 0}]})
    result = fetch_ga4_period(
        client, "seo@x", "properties/1", _DATE_FROM, _DATE_TO,
        {"users": "totalUsers", "conversions": "conversions"},
    )
    assert result["users"] == 0
    assert result["conversions"] == 0


def test_no_rows_at_all_is_none_not_zero():
    client = FakeNinjaClient({}, {})
    result = fetch_ga4_period(
        client, "seo@x", "properties/1", _DATE_FROM, _DATE_TO,
        {"users": "totalUsers", "conversions": "conversions"},
    )
    assert result["users"] is None
    assert result["conversions"] is None


def test_without_channel_field_organic_and_direct_stay_none():
    client = FakeNinjaClient({}, {"ga4:2": [{"totalUsers": 500, "conversions": 10}]})
    result = fetch_ga4_period(
        client, "seo@x", "properties/1", _DATE_FROM, _DATE_TO,
        {"users": "totalUsers", "conversions": "conversions"},
    )
    assert result["users"] == 500
    assert result["organic_users"] is None
    assert result["direct_users"] is None


def test_fetch_gsc_period_sums_rows_and_preserves_zero():
    client = FakeNinjaClient({}, {"google_search_console:2": [{"clicks": 0, "impressions": 100}]})
    result = fetch_gsc_period(
        client, "seo@x", "sc-domain:x.com", _DATE_FROM, _DATE_TO,
        {"clicks": "clicks", "impressions": "impressions"},
    )
    assert result == {"clicks": 0, "impressions": 100}


def test_fetch_gsc_period_uses_web_search_type():
    client = FakeNinjaClient({}, {"google_search_console:2": [{"clicks": 1, "impressions": 1}]})
    fetch_gsc_period(
        client, "seo@x", "sc-domain:x.com", _DATE_FROM, _DATE_TO,
        {"clicks": "clicks", "impressions": "impressions"},
    )
    _integration, _fields, settings = client.calls[-1]
    assert settings == {"search_type": "web"}


def test_fetch_gsc_period_no_rows_is_none_not_zero():
    client = FakeNinjaClient({}, {})
    result = fetch_gsc_period(
        client, "seo@x", "sc-domain:x.com", _DATE_FROM, _DATE_TO,
        {"clicks": "clicks", "impressions": "impressions"},
    )
    assert result == {"clicks": None, "impressions": None}


# --- task-45 §1.6/§2.5: the `date` dimension is the None-vs-0 discriminator ----


def test_discover_gsc_fields_picks_up_the_date_dimension():
    client = FakeNinjaClient({"google_search_console": _GSC_FIELDS}, {})
    assert discover_gsc_fields(client) == _GSC_FIELD_IDS_WITH_DATE


def test_gsc_period_with_a_date_dimension_sums_the_days():
    client = FakeNinjaClient(
        {},
        {
            "google_search_console:3": [
                {"date": "2026-07-01", "clicks": 100, "impressions": 1000},
                {"date": "2026-07-02", "clicks": 28, "impressions": 564},
            ]
        },
    )
    result = fetch_gsc_period(
        client, "seo@x", "sc-domain:x.com", _DATE_FROM, _DATE_TO, _GSC_FIELD_IDS_WITH_DATE
    )
    assert result == {"clicks": 128, "impressions": 1564}
    _integration, fields, settings = client.calls[-1]
    assert fields == ("date", "clicks", "impressions")
    assert settings == {"search_type": "web"}


def test_gsc_period_with_zero_day_rows_is_none_not_zero():
    """The live 3XE finding: asked undimensioned, Reporting Ninja answers a
    period from before the property existed with `{clicks: 0, impressions: 0}`;
    asked by day, it answers with no rows at all (task-45 §1.6).
    """
    client = FakeNinjaClient({}, {"google_search_console:3": []})
    result = fetch_gsc_period(
        client, "seo@x", "sc-domain:x.com", _DATE_FROM, _DATE_TO, _GSC_FIELD_IDS_WITH_DATE
    )
    assert result == {"clicks": None, "impressions": None}


def test_gsc_period_keeps_a_genuinely_zero_day_as_zero():
    client = FakeNinjaClient(
        {}, {"google_search_console:3": [{"date": "2026-07-01", "clicks": 0, "impressions": 0}]}
    )
    result = fetch_gsc_period(
        client, "seo@x", "sc-domain:x.com", _DATE_FROM, _DATE_TO, _GSC_FIELD_IDS_WITH_DATE
    )
    assert result == {"clicks": 0, "impressions": 0}


def test_fetch_gsc_daily_asks_for_more_rows_than_the_window_has_days():
    """A day-dimensioned year is 365 rows; the client's default `limit=100`
    would silently truncate the answer and produce a WRONG sum.
    """
    client = FakeNinjaClient({}, {"google_search_console:3": []})
    fetch_gsc_daily(
        client, "seo@x", "sc-domain:x.com", date(2025, 8, 1), date(2026, 7, 31),
        _GSC_FIELD_IDS_WITH_DATE,
    )
    assert client.limits[-1] > 365


def test_fetch_period_metrics_without_gsc_ref_leaves_clicks_impressions_none():
    client = FakeNinjaClient({}, {"ga4:2": [{"totalUsers": 10, "conversions": 1}]})
    metrics = fetch_period_metrics(
        client,
        ga4_connection_key="seo@x",
        ga4_account_id="properties/1",
        ga4_fields={"users": "totalUsers", "conversions": "conversions"},
        gsc_ref=None,
        gsc_fields=None,
        date_from=_DATE_FROM,
        date_to=_DATE_TO,
    )
    assert metrics.users == 10
    assert metrics.clicks is None
    assert metrics.impressions is None
