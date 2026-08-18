"""Reports engine REST API (task-42 §5): contracts for every endpoint, the
422/404 preflight paths, SSE with `Last-Event-ID`, and `Content-Disposition`
for `inline`/`attachment` file downloads.

The scheduler never runs in these tests (no worker process is started) —
`POST /runs` is only ever checked as far as "queued row created, right
validation happened first"; anything past that (actual execution) is
`app/reports/runner.py`'s and `scheduler.py`'s own test responsibility.
"""

from __future__ import annotations

from datetime import date

from app.db.models import ReportFile
from app.db.repositories.clients import ClientRepository
from app.db.repositories.reports import ReportRepository
from app.reports import ninja_browser
from app.schemas.enums import Provider, SourceMode


def _create_client(db, *, slug: str = "effys", name: str = "EFFYS", with_gsc: bool = False) -> str:
    client = ClientRepository(db).create(
        name=name, slug=slug, domain=f"{slug}.example.com", timezone="America/New_York"
    )
    db.flush()
    ClientRepository(db).upsert_integration(
        client.id, Provider.reporting_ninja, source_mode=SourceMode.api,
        config={"connection_key": "seo@x", "account_id": "properties/1"},
    )
    if with_gsc:
        ClientRepository(db).upsert_integration(
            client.id, Provider.gsc, source_mode=SourceMode.api,
            config={"connection_key": "seo@x", "account_id": "sc-domain:x.com"},
        )
    db.commit()
    return client.id


# --- GET /types ----------------------------------------------------------------


def test_report_types_lists_all_four_kinds_and_the_configured_limits(client):
    response = client.get("/api/v1/reports/types")
    assert response.status_code == 200
    body = response.json()
    kinds = {t["kind"] for t in body["types"]}
    assert kinds == {"monthly", "yearly", "custom_months", "comparator"}
    assert body["max_months"] == 24
    assert body["max_comparator_years"] == 4


# --- POST /runs -------------------------------------------------------------


def test_create_run_404_for_unknown_client(client):
    response = client.post(
        "/api/v1/reports/runs",
        json={"client_id": "nope", "kind": "monthly", "source": "api"},
    )
    assert response.status_code == 404
    assert response.json()["detail"]["error_code"] == "client_not_found"


def test_create_run_422_when_reporting_ninja_is_not_configured(client, db):
    unconfigured = ClientRepository(db).create(
        name="NoRN", slug="norn", domain="norn.example.com", timezone="America/New_York"
    )
    db.commit()
    response = client.post(
        "/api/v1/reports/runs",
        json={"client_id": unconfigured.id, "kind": "monthly", "source": "api"},
    )
    assert response.status_code == 422
    assert response.json()["detail"]["error_code"] == "not_configured"


def test_create_pdf_run_422_when_report_name_is_missing(client, db):
    client_id = _create_client(db)
    response = client.post(
        "/api/v1/reports/runs",
        json={"client_id": client_id, "kind": "monthly", "source": "pdf"},
    )
    assert response.status_code == 422
    assert response.json()["detail"]["error_code"] == "report_name_missing"


def test_create_run_422_for_a_future_custom_month(client, db):
    client_id = _create_client(db)
    future_month = date.today().replace(day=1)
    next_year = f"{future_month.year + 1}-{future_month.month:02d}"
    response = client.post(
        "/api/v1/reports/runs",
        json={
            "client_id": client_id, "kind": "custom_months", "source": "api",
            "months": [next_year],
        },
    )
    assert response.status_code == 422
    assert response.json()["detail"]["error_code"] == "invalid_period"


def test_create_run_422_for_mismatched_comparator_months(client, db):
    client_id = _create_client(db)
    response = client.post(
        "/api/v1/reports/runs",
        json={
            "client_id": client_id, "kind": "comparator", "source": "api",
            "months": ["2025-07", "2024-08"],
        },
    )
    assert response.status_code == 422
    assert response.json()["detail"]["error_code"] == "invalid_comparator_months"


def test_create_run_accepted_and_queued(client, db):
    client_id = _create_client(db)
    response = client.post(
        "/api/v1/reports/runs",
        json={"client_id": client_id, "kind": "monthly", "source": "api"},
    )
    assert response.status_code == 202
    body = response.json()
    assert body["status_url"] == f"/api/v1/reports/runs/{body['run_id']}"
    assert body["events_url"] == f"/api/v1/reports/runs/{body['run_id']}/events"

    followup = client.get(body["status_url"])
    assert followup.status_code == 200
    assert followup.json()["status"] == "queued"
    assert followup.json()["period_count"] == 6


def test_a_queued_run_already_carries_its_labelled_periods(client, db):
    """task-45 §2.1: the period list exists from the moment the run is
    enqueued, so the UI never has to invent `Period 1 … Period 6` placeholders
    while waiting for the first event.
    """
    client_id = _create_client(db)
    run_id = client.post(
        "/api/v1/reports/runs",
        json={"client_id": client_id, "kind": "monthly", "source": "api"},
    ).json()["run_id"]

    periods = client.get(f"/api/v1/reports/runs/{run_id}").json()["periods"]
    assert len(periods) == 6
    assert periods[0]["label"] == "This Month"
    assert all(p["state"] == "waiting" for p in periods)
    assert all(p["date_from"] and p["date_to"] for p in periods)
    assert all(p["duration_seconds"] is None for p in periods)


def test_create_run_rejects_unknown_fields(client, db):
    client_id = _create_client(db)
    response = client.post(
        "/api/v1/reports/runs",
        json={"client_id": client_id, "kind": "monthly", "source": "api", "bogus": 1},
    )
    assert response.status_code == 422


# --- GET /runs (list) --------------------------------------------------------


def test_list_runs_filters_by_client_and_paginates(client, db):
    client_id = _create_client(db)
    other_client_id = _create_client(db, slug="other")
    repo = ReportRepository(db)
    for _ in range(3):
        repo.create_run(client_id, kind="monthly", source="api", params={}, period_count=6)
    repo.create_run(other_client_id, kind="monthly", source="api", params={}, period_count=6)
    db.commit()

    response = client.get(f"/api/v1/reports/runs?client_id={client_id}&limit=2")
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 3
    assert len(body["rows"]) == 2


# --- GET /runs/{id} -----------------------------------------------------------


def test_get_run_404_for_unknown_id(client):
    response = client.get("/api/v1/reports/runs/nope")
    assert response.status_code == 404
    assert response.json()["detail"]["error_code"] == "run_not_found"


# --- GET /runs/{id}/events (SSE) ---------------------------------------------


def test_run_events_stream_includes_every_field_the_frontend_needs(client, db):
    client_id = _create_client(db)
    repo = ReportRepository(db)
    run = repo.create_run(client_id, kind="monthly", source="api", params={}, period_count=6)
    repo.append_event(
        run.id, "info", stage="fetch", message="This Month done",
        details={"period": "This Month"},
    )
    repo.append_event(run.id, "warning", stage="preflight", message="GSC not configured")
    repo.finish(run.id, status="succeeded")
    db.commit()

    response = client.get(f"/api/v1/reports/runs/{run.id}/events")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    body = response.text
    assert "id: 1" in body
    assert "This Month done" in body
    # would KeyError/AttributeError at request time if the SSE adapter were incomplete
    assert '"percent": null' in body
    assert "GSC not configured" in body


def test_run_events_last_event_id_resumes_after_the_given_seq(client, db):
    client_id = _create_client(db)
    repo = ReportRepository(db)
    run = repo.create_run(client_id, kind="monthly", source="api", params={}, period_count=6)
    repo.append_event(run.id, "info", message="first")
    repo.append_event(run.id, "info", message="second")
    repo.finish(run.id, status="succeeded")
    db.commit()

    response = client.get(
        f"/api/v1/reports/runs/{run.id}/events", headers={"Last-Event-ID": "1"}
    )
    assert "first" not in response.text
    assert "second" in response.text


# --- POST /runs/{id}/cancel ---------------------------------------------------


def test_cancel_404_for_unknown_run(client):
    response = client.post("/api/v1/reports/runs/nope/cancel")
    assert response.status_code == 404


def test_cancel_409_for_an_already_finished_run(client, db):
    client_id = _create_client(db)
    repo = ReportRepository(db)
    run = repo.create_run(client_id, kind="monthly", source="api", params={}, period_count=6)
    repo.finish(run.id, status="succeeded")
    db.commit()

    response = client.post(f"/api/v1/reports/runs/{run.id}/cancel")
    assert response.status_code == 409
    assert response.json()["detail"]["error_code"] == "run_not_cancellable"


def test_cancel_marks_a_queued_run(client, db):
    client_id = _create_client(db)
    repo = ReportRepository(db)
    run = repo.create_run(client_id, kind="monthly", source="api", params={}, period_count=6)
    db.commit()

    response = client.post(f"/api/v1/reports/runs/{run.id}/cancel")
    assert response.status_code == 200
    assert response.json()["cancel_requested"] is True


# --- GET /runs/{id}/table ------------------------------------------------------


def test_get_table_404_before_the_run_has_a_result(client, db):
    client_id = _create_client(db)
    repo = ReportRepository(db)
    run = repo.create_run(client_id, kind="monthly", source="api", params={}, period_count=6)
    db.commit()

    response = client.get(f"/api/v1/reports/runs/{run.id}/table")
    assert response.status_code == 404


def test_get_table_returns_the_stored_result(client, db):
    client_id = _create_client(db)
    repo = ReportRepository(db)
    run = repo.create_run(client_id, kind="monthly", source="api", params={}, period_count=6)
    repo.finish(
        run.id, status="succeeded",
        result={
            "periods": [
                {
                    "label": "This Month", "date_from": "2026-07-01",
                    "date_to": "2026-07-31", "is_partial": False,
                }
            ],
            "change_labels": [],
            "rows": [{"key": "users", "label": "Users", "values": [6251], "changes": []}],
            "sources": {},
        },
    )
    db.commit()

    response = client.get(f"/api/v1/reports/runs/{run.id}/table")
    assert response.status_code == 200
    body = response.json()
    assert body["rows"][0]["values"] == [6251]
    assert body["periods"][0]["label"] == "This Month"
    assert body["history"] is None  # a pre-task-45 result never invents one


def test_get_table_returns_the_history_block_when_the_run_collected_one(client, db):
    client_id = _create_client(db)
    repo = ReportRepository(db)
    run = repo.create_run(client_id, kind="monthly", source="api", params={}, period_count=1)
    repo.finish(
        run.id, status="succeeded",
        result={
            "periods": [
                {
                    "label": "This Month", "date_from": "2026-07-01",
                    "date_to": "2026-07-31", "is_partial": False,
                }
            ],
            "change_labels": [],
            "rows": [{"key": "clicks", "label": "Clicks", "values": [2528], "changes": []}],
            "sources": {},
            "history": {
                "months": [
                    {
                        "month": "2026-07", "label": "July 2026", "clicks": 2528,
                        "impressions": 31564, "ctr": 0.0801, "organic_conversions": 654,
                    },
                    {
                        "month": "2025-01", "label": "January 2025", "clicks": None,
                        "impressions": None, "ctr": None, "organic_conversions": None,
                    },
                ],
                "has_gsc": True,
                "has_ga4": True,
            },
        },
    )
    db.commit()

    body = client.get(f"/api/v1/reports/runs/{run.id}/table").json()
    months = body["history"]["months"]
    # The overlapping month equals the main table's own column, to the unit.
    assert months[0]["clicks"] == body["rows"][0]["values"][0]
    assert months[0]["organic_conversions"] == 654
    assert months[1]["ctr"] is None


# --- GET /files/{id} -----------------------------------------------------------


def test_download_file_404_for_unknown_id(client):
    response = client.get("/api/v1/reports/files/nope")
    assert response.status_code == 404


def test_download_file_inline_vs_attachment_disposition(client, db, tmp_path):
    from app.core.config import get_settings

    settings = get_settings()
    client_id = _create_client(db)
    pdf_path = settings.artifacts_dir / "reports" / "x.pdf"
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    pdf_path.write_bytes(b"%PDF-1.4 fake" + b"0" * 100)

    file = ReportFile(
        client_id=client_id, kind="pdf",
        path=str(pdf_path.relative_to(settings.artifacts_dir)),
        sha256="x" * 64, size_bytes=pdf_path.stat().st_size, mime="application/pdf",
    )
    db.add(file)
    db.commit()

    inline = client.get(f"/api/v1/reports/files/{file.id}?disposition=inline")
    assert inline.status_code == 200
    assert "inline" in inline.headers["content-disposition"]

    attachment = client.get(f"/api/v1/reports/files/{file.id}")
    assert attachment.status_code == 200
    assert "attachment" in attachment.headers["content-disposition"]


# --- GET/PUT /clients/{id}/ninja-report ---------------------------------------


def test_get_ninja_report_name_404_for_unknown_client(client):
    response = client.get("/api/v1/reports/clients/nope/ninja-report")
    assert response.status_code == 404


def test_get_ninja_report_name_skips_the_browser_without_credentials(client, db, monkeypatch):
    monkeypatch.delenv("REPORTING_NINJA_EMAIL", raising=False)
    monkeypatch.delenv("REPORTING_NINJA_PASSWORD", raising=False)
    client_id = _create_client(db)

    response = client.get(f"/api/v1/reports/clients/{client_id}/ninja-report")
    assert response.status_code == 200
    body = response.json()
    assert body["ninja_report_name"] is None
    assert body["matches"] == []
    assert body["others"] == []


def _fake_account(monkeypatch, names: list[str], *, truncated: bool = False) -> None:
    monkeypatch.setenv("REPORTING_NINJA_EMAIL", "ops@example.com")
    monkeypatch.setenv("REPORTING_NINJA_PASSWORD", "secret")
    monkeypatch.setattr(
        "app.api.reports.ninja_browser.list_report_names",
        lambda **_kwargs: (names, truncated),
    )

    def fake_discover(*, client_name: str, **_kwargs):
        return ninja_browser.ReportDiscovery(
            matches=[n for n in names if _name_matches(n, client_name)],
            all_names=names,
            truncated=truncated,
        )

    monkeypatch.setattr("app.api.reports.ninja_browser.discover_report_names", fake_discover)


def _name_matches(report_name: str, client_name: str) -> bool:
    return report_name.upper().startswith(client_name.upper())


def test_discovery_splits_this_clients_reports_from_the_rest_of_the_account(
    client, db, monkeypatch
):
    """task-45 §1.2: the live account holds 32 reports for ~30 different
    clients. task-42 returned all of them as one list where the client-name
    match only affected sort order, so binding a foreign report was one
    mis-click away.
    """
    client_id = _create_client(db)  # client name is EFFYS
    _fake_account(monkeypatch, ["ADA-client", "EFFYS-Internal report", "SAPHNY-client"])

    body = client.get(f"/api/v1/reports/clients/{client_id}/ninja-report").json()
    assert [c["name"] for c in body["matches"]] == ["EFFYS-Internal report"]
    assert [c["name"] for c in body["others"]] == ["ADA-client", "SAPHNY-client"]
    assert body["client_name"] == "EFFYS"


def test_put_rejects_a_foreign_report_without_explicit_confirmation(client, db, monkeypatch):
    client_id = _create_client(db)
    _fake_account(monkeypatch, ["EFFYS-Internal report", "SAPHNY-client"])

    response = client.put(
        f"/api/v1/reports/clients/{client_id}/ninja-report",
        json={"ninja_report_name": "SAPHNY-client"},
    )
    assert response.status_code == 422
    assert response.json()["detail"]["error_code"] == "report_name_mismatch"
    integration = ClientRepository(db).get_integration(client_id, Provider.reporting_ninja)
    assert "ninja_report_name" not in integration.config_json  # nothing was written


def test_put_accepts_a_foreign_report_when_confirmed(client, db, monkeypatch):
    client_id = _create_client(db)
    _fake_account(monkeypatch, ["EFFYS-Internal report", "SAPHNY-client"])

    response = client.put(
        f"/api/v1/reports/clients/{client_id}/ninja-report",
        json={"ninja_report_name": "SAPHNY-client", "confirm_mismatch": True},
    )
    assert response.status_code == 200
    assert response.json()["ninja_report_name"] == "SAPHNY-client"


def test_put_rejects_a_name_the_account_does_not_have(client, db, monkeypatch):
    client_id = _create_client(db)
    _fake_account(monkeypatch, ["EFFYS-Internal report"])

    response = client.put(
        f"/api/v1/reports/clients/{client_id}/ninja-report",
        json={"ninja_report_name": "EFFYS-typo report", "confirm_mismatch": True},
    )
    assert response.status_code == 422
    assert response.json()["detail"]["error_code"] == "report_name_unknown"


def test_discovery_marks_a_truncated_full_list(client, db, monkeypatch):
    # task-48 §2.1: the scroll pass ran out of iteration budget before the
    # set of names stabilized — the UI must be told this list is not proof
    # of anything absent from it.
    client_id = _create_client(db)
    _fake_account(monkeypatch, ["EFFYS-Internal report", "ADA-client"], truncated=True)

    body = client.get(f"/api/v1/reports/clients/{client_id}/ninja-report").json()
    assert body["others_truncated"] is True


def test_discovery_finds_a_match_the_full_list_missed(client, db, monkeypatch):
    # task-48 §1.6 live case: a client's report sorts past the cabinet
    # table's cutoff, so a full scroll pass never lists it. The targeted
    # search (`discover_report_names`'s `matches`) must still surface it.
    client_id = _create_client(db)  # client name is EFFYS

    def fake_discover(*, client_name: str, **_kwargs):
        return ninja_browser.ReportDiscovery(
            matches=["EFFYS-Internal report"], all_names=["ADA-client"], truncated=True,
        )

    monkeypatch.setenv("REPORTING_NINJA_EMAIL", "ops@example.com")
    monkeypatch.setenv("REPORTING_NINJA_PASSWORD", "secret")
    monkeypatch.setattr("app.api.reports.ninja_browser.discover_report_names", fake_discover)

    body = client.get(f"/api/v1/reports/clients/{client_id}/ninja-report").json()
    assert [c["name"] for c in body["matches"]] == ["EFFYS-Internal report"]
    assert body["others_truncated"] is True


def test_put_never_rejects_a_name_from_a_truncated_cabinet_list(client, db, monkeypatch):
    # task-48 §1.7/DoD: an unproven absence from a truncated scroll must
    # never become `report_name_unknown` — this is exactly the SWM-style
    # trap (task-48 §1.6): a real client whose name sorts past the cutoff.
    client_id = _create_client(db)
    _fake_account(monkeypatch, ["ADA-client"], truncated=True)

    response = client.put(
        f"/api/v1/reports/clients/{client_id}/ninja-report",
        json={"ninja_report_name": "EFFYS-Internal report"},
    )
    assert response.status_code == 200
    assert response.json()["ninja_report_name"] == "EFFYS-Internal report"


def test_put_ninja_report_name_saves_and_preserves_the_connection(client, db, monkeypatch):
    # This test is about the DB-save mechanics only, not discovery — isolate
    # it from whatever REPORTING_NINJA_EMAIL/PASSWORD happen to be set in the
    # ambient environment (found live: a real `.env` bind-mount made this
    # test launch an actual Playwright login against reportingninja.com and
    # time out, the same real-credentials trap
    # `test_get_ninja_report_name_skips_the_browser_without_credentials`
    # already guards against for the sibling GET test).
    monkeypatch.delenv("REPORTING_NINJA_EMAIL", raising=False)
    monkeypatch.delenv("REPORTING_NINJA_PASSWORD", raising=False)
    client_id = _create_client(db)
    response = client.put(
        f"/api/v1/reports/clients/{client_id}/ninja-report",
        json={"ninja_report_name": "EFFYS-Internal report"},
    )
    assert response.status_code == 200
    assert response.json()["ninja_report_name"] == "EFFYS-Internal report"

    integration = ClientRepository(db).get_integration(client_id, Provider.reporting_ninja)
    assert integration.config_json["ninja_report_name"] == "EFFYS-Internal report"
    assert integration.config_json["connection_key"] == "seo@x"  # untouched


def test_put_ninja_report_name_422_when_not_configured(client, db):
    unconfigured = ClientRepository(db).create(
        name="NoRN2", slug="norn2", domain="norn2.example.com", timezone="America/New_York"
    )
    db.commit()
    response = client.put(
        f"/api/v1/reports/clients/{unconfigured.id}/ninja-report",
        json={"ninja_report_name": "X"},
    )
    assert response.status_code == 422
    assert response.json()["detail"]["error_code"] == "not_configured"


# --- GET /accuracy (task-48 §2.4) --------------------------------------------


def _cross_check_result(client_id: str, db, *, cells: list[dict]) -> None:
    """Seed a finished `source=pdf` run carrying a stored cross-check, the
    shape `list_client_accuracy` reads back (task-48 §2.4).
    """
    repo = ReportRepository(db)
    run = repo.create_run(client_id, kind="monthly", source="pdf", params={}, period_count=1)
    status = "mismatch" if any(c["verdict"] in ("mismatch", "missing_api", "missing_pdf")
                                for c in cells) else "match"
    repo.finish(
        run.id, status="succeeded", cross_check_status=status,
        result={
            "periods": [], "change_labels": [], "rows": [], "sources": {},
            "cross_check": {"status": status, "cells": cells},
        },
    )
    db.commit()


def test_accuracy_report_lists_every_client_including_unconfigured_ones(client, db):
    _create_client(db, slug="effys")
    ClientRepository(db).create(
        name="NoRN3", slug="norn3", domain="norn3.example.com", timezone="America/New_York"
    )
    db.commit()

    body = client.get("/api/v1/reports/accuracy").json()
    by_name = {row["client_name"]: row for row in body["clients"]}
    assert by_name["EFFYS"]["binding_status"] == "missing"  # configured, no report name saved
    assert by_name["NoRN3"]["binding_status"] == "not_configured"
    assert by_name["NoRN3"]["latest_pdf_run_id"] is None


def test_accuracy_report_flags_a_foreign_binding(client, db):
    client_id = _create_client(db)  # client name EFFYS
    ClientRepository(db).upsert_integration(
        client_id, Provider.reporting_ninja, source_mode=SourceMode.api,
        config={"connection_key": "seo@x", "account_id": "properties/1",
                "ninja_report_name": "SWM-Internal report"},
    )
    db.commit()

    body = client.get("/api/v1/reports/accuracy").json()
    [row] = [r for r in body["clients"] if r["client_name"] == "EFFYS"]
    assert row["binding_status"] == "foreign"
    assert row["ninja_report_name"] == "SWM-Internal report"


def test_accuracy_report_summarizes_the_latest_pdf_runs_cross_check(client, db):
    # task-48 §1: the real EFFYS shape — mostly match, a few genuinely
    # no-data, nothing that actually differs.
    client_id = _create_client(db)
    cells = (
        [{"metric_key": "users", "verdict": "match"}] * 44
        + [{"metric_key": "clicks", "verdict": "no_data"}] * 4
    )
    _cross_check_result(client_id, db, cells=cells)

    body = client.get("/api/v1/reports/accuracy").json()
    [row] = [r for r in body["clients"] if r["client_name"] == "EFFYS"]
    assert (row["match_count"], row["no_data_count"], row["differ_count"]) == (44, 4, 0)
    assert row["latest_pdf_run_id"] is not None


def test_accuracy_report_counts_missing_api_and_missing_pdf_as_differ(client, db):
    client_id = _create_client(db)
    cells = [
        {"metric_key": "users", "verdict": "match"},
        {"metric_key": "clicks", "verdict": "missing_api"},
        {"metric_key": "impressions", "verdict": "missing_pdf"},
        {"metric_key": "conversions", "verdict": "mismatch"},
    ]
    _cross_check_result(client_id, db, cells=cells)

    body = client.get("/api/v1/reports/accuracy").json()
    [row] = [r for r in body["clients"] if r["client_name"] == "EFFYS"]
    assert (row["match_count"], row["no_data_count"], row["differ_count"]) == (1, 0, 3)


def test_accuracy_report_reflects_gsc_coverage(client, db):
    with_gsc = _create_client(db, slug="withgsc", name="WithGSC", with_gsc=True)
    without_gsc = _create_client(db, slug="withoutgsc", name="WithoutGSC")

    body = client.get("/api/v1/reports/accuracy").json()
    by_id = {row["client_id"]: row for row in body["clients"]}
    assert by_id[with_gsc]["has_gsc"] is True
    assert by_id[without_gsc]["has_gsc"] is False
