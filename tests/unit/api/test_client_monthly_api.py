"""Monthly Performance table REST API (task-54): the 4 endpoint contracts,
`not_configured` vs `client_not_found`, and the non-blocking
`attribution_changed` warning.

`app.api.client_monthly.ReportingNinjaClient` is monkeypatched to a fake for
every test that reaches `/refresh` or `/key-events` — the real class would
otherwise make live HTTP calls against whatever `REPORTING_NINJA_API_KEY`
happens to be set in this container's environment (same reasoning as
`test_report_runner.py`'s own `FakeNinjaClient` monkeypatch).
"""

from __future__ import annotations

import app.api.client_monthly as client_monthly_api
from app.db.repositories.clients import ClientRepository
from app.schemas.enums import Provider, SourceMode

_GA4_FIELDS = [
    {"field_id": "yearMonth"},
    {"field_id": "totalUsers"},
    {"field_id": "conversions"},
    {"field_id": "firstUserDefaultChannelGroup"},
    {"field_id": "sessionDefaultChannelGroup"},
    {"field_id": "eventName"},
]
_GSC_FIELDS = [{"field_id": "clicks"}, {"field_id": "impressions"}, {"field_id": "date"}]


class FakeNinjaClient:
    """Context-manager-compatible fake — `app/api/client_monthly.py` uses
    `with ReportingNinjaClient(api_key) as client_rn:`.
    """

    def __init__(self, *_args, **_kwargs):
        self.request_count = 0
        self.calls: list[tuple] = []

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def list_fields(self, integration_id: str) -> list[dict]:
        return _GA4_FIELDS if integration_id == "ga4" else _GSC_FIELDS

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
        self.calls.append((integration_id, key))
        if integration_id == "ga4" and key == (
            "yearMonth",
            "firstUserDefaultChannelGroup",
            "totalUsers",
        ):
            return [
                {
                    "yearMonth": "202607",
                    "firstUserDefaultChannelGroup": "Organic Search",
                    "totalUsers": 721,
                }
            ]
        if integration_id == "ga4" and key == (
            "yearMonth",
            "sessionDefaultChannelGroup",
            "conversions",
        ):
            return [
                {
                    "yearMonth": "202607",
                    "sessionDefaultChannelGroup": "Organic Search",
                    "conversions": 109,
                }
            ]
        if integration_id == "google_search_console":
            return [{"date": "2026-07-15", "clicks": 717, "impressions": 69176}]
        return []


def _seed_client(
    db, *, with_gsc: bool = False, with_rn: bool = True, slug: str = "sds", name: str = "SDS"
) -> str:
    repo = ClientRepository(db)
    client = repo.create(
        name=name, slug=slug, domain=f"{slug}.example.com", timezone="America/New_York"
    )
    db.flush()
    if with_rn:
        repo.upsert_integration(
            client.id,
            Provider.reporting_ninja,
            source_mode=SourceMode.api,
            config={"connection_key": "seo@x", "account_id": "properties/1"},
        )
    if with_gsc:
        repo.upsert_integration(
            client.id,
            Provider.gsc,
            source_mode=SourceMode.api,
            config={"connection_key": "seo@x", "account_id": "sc-domain:x.com"},
        )
    db.commit()
    return client.id


# --- GET (read-only, never touches the provider) ----------------------------


def test_get_table_404_for_unknown_client(client):
    response = client.get("/api/v1/clients/nope/monthly-table")
    assert response.status_code == 404
    assert response.json()["detail"]["error_code"] == "client_not_found"


def test_get_table_before_any_refresh_is_honestly_empty(client, db):
    client_id = _seed_client(db, with_gsc=True)
    response = client.get(f"/api/v1/clients/{client_id}/monthly-table")
    assert response.status_code == 200
    body = response.json()
    assert len(body["rows"]) > 0
    assert all(row["organic_users"] is None for row in body["rows"])
    assert all(row["ga4_state"] == "no_data" for row in body["rows"])
    assert body["coverage"]["has_gsc"] is True
    assert body["coverage"]["has_key_event_column"] is False


# --- POST /refresh -----------------------------------------------------------


def test_refresh_409_when_client_has_no_reporting_ninja_integration(client, db):
    client_id = _seed_client(db, with_rn=False)
    response = client.post(f"/api/v1/clients/{client_id}/monthly-table/refresh")
    assert response.status_code == 409
    assert response.json()["detail"]["error_code"] == "not_configured"


def test_refresh_404_for_unknown_client(client):
    response = client.post("/api/v1/clients/nope/monthly-table/refresh")
    assert response.status_code == 404


def test_refresh_populates_rows_from_the_provider(client, db, monkeypatch):
    monkeypatch.setenv("REPORTING_NINJA_API_KEY", "fake-api-key")
    monkeypatch.setattr(client_monthly_api, "ReportingNinjaClient", FakeNinjaClient)
    client_id = _seed_client(db, with_gsc=True)

    response = client.post(f"/api/v1/clients/{client_id}/monthly-table/refresh")
    assert response.status_code == 200
    body = response.json()
    row = next(r for r in body["table"]["rows"] if r["month"] == "2026-07")
    assert row["organic_users"] == 721
    assert row["organic_conversions"] == 109
    assert row["gsc_clicks"] == 717
    assert body["requests_used"] > 0


def test_refresh_without_force_skips_frozen_months_on_the_second_call(client, db, monkeypatch):
    monkeypatch.setenv("REPORTING_NINJA_API_KEY", "fake-api-key")
    monkeypatch.setattr(client_monthly_api, "ReportingNinjaClient", FakeNinjaClient)
    client_id = _seed_client(db, with_gsc=True)

    first = client.post(f"/api/v1/clients/{client_id}/monthly-table/refresh")
    assert first.json()["requests_used"] > 0

    second = client.post(f"/api/v1/clients/{client_id}/monthly-table/refresh")
    # Every month collected by `first` is now well past the 3-day settle
    # window relative to real "today" (they are historical, not the
    # just-closed month) — a plain refresh must not re-query them.
    assert second.json()["requests_used"] == 0
    assert second.json()["months_collected"] == []


def test_refresh_force_all_recollects_even_frozen_months(client, db, monkeypatch):
    monkeypatch.setenv("REPORTING_NINJA_API_KEY", "fake-api-key")
    monkeypatch.setattr(client_monthly_api, "ReportingNinjaClient", FakeNinjaClient)
    client_id = _seed_client(db, with_gsc=True)

    client.post(f"/api/v1/clients/{client_id}/monthly-table/refresh")
    forced = client.post(f"/api/v1/clients/{client_id}/monthly-table/refresh?force=all")
    assert forced.json()["requests_used"] > 0


# --- GET /key-events ----------------------------------------------------------


def test_key_events_409_when_not_configured(client, db):
    client_id = _seed_client(db, with_rn=False)
    response = client.get(f"/api/v1/clients/{client_id}/monthly-table/key-events")
    assert response.status_code == 409


def test_key_events_lists_candidates(client, db, monkeypatch):
    monkeypatch.setenv("REPORTING_NINJA_API_KEY", "fake-api-key")

    class EventNinjaClient(FakeNinjaClient):
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
            if integration_id == "ga4" and "eventName" in fields:
                return [
                    {
                        "yearMonth": "202607",
                        "eventName": "Book Now",
                        "sessionDefaultChannelGroup": "Organic Search",
                        "conversions": 70,
                    },
                ]
            return []

    monkeypatch.setattr(client_monthly_api, "ReportingNinjaClient", EventNinjaClient)
    client_id = _seed_client(db)

    response = client.get(f"/api/v1/clients/{client_id}/monthly-table/key-events")
    assert response.status_code == 200
    candidates = response.json()["candidates"]
    assert any(c["event_name"] == "Book Now" for c in candidates)


# --- PUT /settings ------------------------------------------------------------


def test_put_settings_rejects_unknown_fields(client, db):
    client_id = _seed_client(db)
    response = client.put(
        f"/api/v1/clients/{client_id}/monthly-table/settings", json={"bogus_field": True}
    )
    assert response.status_code == 422


def test_put_settings_configures_a_multi_event_column(client, db):
    client_id = _seed_client(db)
    response = client.put(
        f"/api/v1/clients/{client_id}/monthly-table/settings",
        json={
            "key_event_names": ["Book Now 3rd Avenue", "Book Now Hudson"],
            "key_event_label": "Book Now",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["key_event_names"] == ["Book Now 3rd Avenue", "Book Now Hudson"]
    assert body["key_event_label"] == "Book Now"
    assert body["attribution_users"] == "first_user"  # unchanged default
    assert body["attribution_events"] == "session"  # unchanged default


def test_put_settings_404_for_unknown_client(client):
    response = client.put("/api/v1/clients/nope/monthly-table/settings", json={})
    assert response.status_code == 404


def test_put_settings_attribution_change_is_409_but_still_saves(client, db, monkeypatch):
    monkeypatch.setenv("REPORTING_NINJA_API_KEY", "fake-api-key")
    monkeypatch.setattr(client_monthly_api, "ReportingNinjaClient", FakeNinjaClient)
    client_id = _seed_client(db, with_gsc=True)
    client.post(f"/api/v1/clients/{client_id}/monthly-table/refresh")

    response = client.put(
        f"/api/v1/clients/{client_id}/monthly-table/settings",
        json={"attribution_users": "session"},
    )
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["error_code"] == "attribution_changed"
    assert len(detail["details"]["affected_months"]) > 0

    # Non-blocking: the setting really was persisted despite the 409.
    after = client.get(f"/api/v1/clients/{client_id}/monthly-table")
    assert after.json()["settings"]["attribution_users"] == "session"


def test_put_settings_no_attribution_change_is_a_plain_200(client, db):
    client_id = _seed_client(db)
    response = client.put(
        f"/api/v1/clients/{client_id}/monthly-table/settings",
        json={"key_event_label": "Book Now"},
    )
    assert response.status_code == 200


# --- GET /xlsx ("точ в точ" export, task-54 §7) -------------------------------


def test_xlsx_export_404_for_unknown_client(client):
    response = client.get("/api/v1/clients/nope/monthly-table/xlsx")
    assert response.status_code == 404


def test_xlsx_export_returns_a_real_workbook(client, db, monkeypatch):
    monkeypatch.setenv("REPORTING_NINJA_API_KEY", "fake-api-key")
    monkeypatch.setattr(client_monthly_api, "ReportingNinjaClient", FakeNinjaClient)
    client_id = _seed_client(db, with_gsc=True)
    client.post(f"/api/v1/clients/{client_id}/monthly-table/refresh")

    response = client.get(f"/api/v1/clients/{client_id}/monthly-table/xlsx")
    assert response.status_code == 200
    assert response.headers["content-type"] == (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    assert "attachment" in response.headers["content-disposition"]
    assert len(response.content) > 1000


def test_xlsx_export_works_with_no_data_collected_yet(client, db):
    # A client that has never been refreshed still gets a valid (empty) file
    # — never a 500 (PLAN §12: absent data is a state, not an error).
    client_id = _seed_client(db)
    response = client.get(f"/api/v1/clients/{client_id}/monthly-table/xlsx")
    assert response.status_code == 200
    assert len(response.content) > 0
