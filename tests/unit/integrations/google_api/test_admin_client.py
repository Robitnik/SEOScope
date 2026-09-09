"""Ga4AdminClient — property info + key events (task-89 §4.2/§10)."""

from __future__ import annotations

import httpx
import pytest

from app.integrations.google_api.admin_client import Ga4AdminClient
from app.integrations.google_api.errors import GoogleApiPermissionError

from .fakes import FakeGoogle

PROP = "properties/395442288"


def test_property_info_maps_fields(fake_google: FakeGoogle) -> None:
    fake_google.on(
        "analyticsadmin.googleapis.com",
        httpx.Response(
            200,
            json={
                "name": PROP,
                "displayName": "Hear Well Services",
                "timeZone": "America/Los_Angeles",
                "currencyCode": "USD",
                "industryCategory": "HEALTHCARE",
            },
        ),
    )
    info = Ga4AdminClient(fake_google.session()).property_info(PROP)
    assert info.property_id == PROP
    assert info.display_name == "Hear Well Services"
    assert info.time_zone == "America/Los_Angeles"
    assert info.currency_code == "USD"
    assert info.industry_category == "HEALTHCARE"


def test_property_info_uses_get_not_post(fake_google: FakeGoogle) -> None:
    fake_google.on("analyticsadmin.googleapis.com", httpx.Response(200, json={"name": PROP}))
    Ga4AdminClient(fake_google.session()).property_info(PROP)
    assert fake_google.requests[-1].method == "GET"


def test_key_events_maps_and_skips_unnamed(fake_google: FakeGoogle) -> None:
    fake_google.on(
        "keyEvents",
        httpx.Response(
            200,
            json={
                "keyEvents": [
                    {"eventName": "purchase", "createTime": "2023-07-11T16:51:08Z",
                     "custom": False},
                    {"eventName": "Phone", "createTime": "2024-03-15T18:16:13Z", "custom": True},
                    {"createTime": "2024-01-01T00:00:00Z"},  # no eventName -> skipped
                ]
            },
        ),
    )
    events = Ga4AdminClient(fake_google.session()).key_events(PROP)
    assert [e.event_name for e in events] == ["purchase", "Phone"]
    assert events[0].custom is False
    assert events[1].custom is True


def test_key_events_empty_list_is_valid_not_an_error(fake_google: FakeGoogle) -> None:
    fake_google.on("keyEvents", httpx.Response(200, json={}))
    events = Ga4AdminClient(fake_google.session()).key_events(PROP)
    assert events == []


def test_permission_denied_propagates(fake_google: FakeGoogle) -> None:
    fake_google.on(
        "analyticsadmin.googleapis.com",
        httpx.Response(403, json={"error": {"status": "PERMISSION_DENIED", "message": "no"}}),
    )
    with pytest.raises(GoogleApiPermissionError):
        Ga4AdminClient(fake_google.session()).property_info(PROP)
