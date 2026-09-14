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

import pytest

from app.db.models import ReportFile
from app.db.repositories.clients import ClientRepository
from app.db.repositories.reports import ReportRepository
from app.db.repositories.users import UserRepository
from app.db.session import worker_session_scope
from app.reports import ninja_browser, ninja_catalog
from app.reports.batch import reconcile_batches
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


def _fake_account(
    monkeypatch,
    names: list[str],
    *,
    truncated: bool = False,
    fetch_error: str | None = None,
    names_after_refresh: list[str] | None = None,
) -> list[str]:
    """Fake the CABINET, not the endpoint: every per-client answer now comes
    from one shared account snapshot (`ninja_catalog`), so the only thing a
    test has to stand in for is the single scroll pass that fills it.

    Returns the list of cabinet passes actually performed, which is what the
    caching behaviour is asserted on. `names_after_refresh` makes the second
    pass see something the first did not — the operator who creates a report
    in Reporting Ninja while the snapshot is already cached.

    The old helper had `names` and `searchable` as separate knobs, because a
    scroll pass and a search of the same cabinet genuinely disagreed. They no
    longer do: that disagreement was two cabinet sessions running at once
    silently disabling each other's filter, and one lock-holding pass reads
    all 32 names (measured 2026-09-07, `ninja_browser.cabinet_lock`).
    """
    monkeypatch.setenv("REPORTING_NINJA_EMAIL", "ops@example.com")
    monkeypatch.setenv("REPORTING_NINJA_PASSWORD", "secret")
    # An in-memory stand-in for the on-disk snapshot: the real TTL/refresh
    # logic runs, but no test ever touches the deployed cache file.
    cache: dict[str, ninja_catalog.AccountReports] = {}
    monkeypatch.setattr(ninja_catalog, "_read_cache", lambda: cache.get("snapshot"))
    monkeypatch.setattr(
        ninja_catalog, "_write_cache", lambda snapshot: cache.__setitem__("snapshot", snapshot)
    )

    passes: list[str] = []

    def fake_list_report_names(**_kwargs):
        passes.append("cabinet-pass")
        if fetch_error is not None:
            raise RuntimeError(fetch_error)
        if names_after_refresh is not None and len(passes) > 1:
            return list(names_after_refresh), truncated
        return list(names), truncated

    monkeypatch.setattr(ninja_browser, "list_report_names", fake_list_report_names)
    return passes


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


def test_discovery_does_not_report_an_unreadable_cabinet_as_an_empty_account(
    client, db, monkeypatch
):
    """A cabinet pass that FAILED must not arrive as "this client has no
    report". The UI's next line on an empty answer is "Create the client's
    report in Reporting Ninja first" — advice that is actively wrong, and
    acting on it means a duplicate report in the account.

    This replaces task-49's "a scroll list that search proves incomplete":
    its premise (a scroll and a search of the same cabinet disagreeing) was
    the concurrency defect, not a property of the cabinet. The invariant it
    was really protecting — never present an unproven absence as a fact
    (task-48 §1.7) — is what is asserted here.
    """
    client_id = _create_client(db)
    _fake_account(monkeypatch, [], fetch_error="cabinet session is not live")

    body = client.get(f"/api/v1/reports/clients/{client_id}/ninja-report").json()
    assert body["matches"] == []
    assert body["others"] == []
    assert body["lookup_error"] is not None  # …so the UI can say which of the two it is
    assert body["others_truncated"] is True


def test_a_failed_refresh_keeps_serving_the_last_good_snapshot(client, db, monkeypatch):
    # A stale list of real names beats an empty one that reads as "the
    # account has no reports".
    client_id = _create_client(db)  # client name is EFFYS
    _fake_account(monkeypatch, ["ADA-client", "EFFYS-Internal report"])
    client.get(f"/api/v1/reports/clients/{client_id}/ninja-report")

    monkeypatch.setattr(
        ninja_browser,
        "list_report_names",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("cabinet busy")),
    )
    body = client.get(
        f"/api/v1/reports/clients/{client_id}/ninja-report?refresh=true"
    ).json()
    assert [c["name"] for c in body["matches"]] == ["EFFYS-Internal report"]
    assert body["lookup_error"] is not None


def test_one_cabinet_pass_answers_every_client(client, db, monkeypatch):
    """The cost fix. Opening `Connect` used to launch its own Chromium, log
    in and re-read the same cabinet PER CLIENT — and strictly one at a time,
    because two cabinet sessions corrupt each other. Fifteen disconnected
    clients meant fifteen logins, then one more for every save.
    """
    first = _create_client(db)
    second = _create_client(db, slug="ada", name="ADA")
    passes = _fake_account(monkeypatch, ["ADA-client", "EFFYS-Internal report"])

    for client_id in (first, second, first):
        assert client.get(f"/api/v1/reports/clients/{client_id}/ninja-report").status_code == 200
    client.put(
        f"/api/v1/reports/clients/{first}/ninja-report",
        json={"ninja_report_name": "EFFYS-Internal report"},
    )
    assert passes == ["cabinet-pass"]


def test_refresh_true_re_reads_the_cabinet(client, db, monkeypatch):
    client_id = _create_client(db)
    passes = _fake_account(monkeypatch, ["EFFYS-Internal report"])

    client.get(f"/api/v1/reports/clients/{client_id}/ninja-report")
    client.get(f"/api/v1/reports/clients/{client_id}/ninja-report?refresh=true")
    assert passes == ["cabinet-pass", "cabinet-pass"]


def test_put_saves_a_report_created_after_the_snapshot_was_taken(client, db, monkeypatch):
    """The live symptom this whole change started from: saving `SDS-client`
    failed with `report_name_unknown` on a name the account really has.

    A name missing from the snapshot is never rejected on that alone — the
    cabinet is re-read once first, because "the operator created this report
    minutes ago" is the ordinary case. This replaces task-49's "saves a name
    the scroll list never listed", whose mechanism (validate via search
    instead of the scroll) was a workaround for the concurrency defect.
    """
    client_id = _create_client(db)
    passes = _fake_account(
        monkeypatch,
        ["ADA-client"],  # the cached snapshot, taken before the report existed
        names_after_refresh=["ADA-client", "EFFYS-Internal report"],
    )
    client.get(f"/api/v1/reports/clients/{client_id}/ninja-report")

    response = client.put(
        f"/api/v1/reports/clients/{client_id}/ninja-report",
        json={"ninja_report_name": "EFFYS-Internal report"},
    )
    assert response.status_code == 200
    assert response.json()["ninja_report_name"] == "EFFYS-Internal report"
    assert passes == ["cabinet-pass", "cabinet-pass"]  # one re-read, then trusted


def test_put_without_credentials_cannot_verify_and_does_not_reject(client, db, monkeypatch):
    # No credentials means no browser, so the existence question cannot be
    # asked at all — "could not verify" is never a rejection (task-48 §1.7).
    client_id = _create_client(db)
    monkeypatch.delenv("REPORTING_NINJA_EMAIL", raising=False)
    monkeypatch.delenv("REPORTING_NINJA_PASSWORD", raising=False)

    response = client.put(
        f"/api/v1/reports/clients/{client_id}/ninja-report",
        json={"ninja_report_name": "EFFYS-Internal report"},
    )
    assert response.status_code == 200


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


# --- GET /ninja-bindings (task-49 §2.1) -------------------------------------


def _explode(*_a, **_k):
    raise AssertionError("must not touch the Reporting Ninja browser")


def test_ninja_bindings_reports_all_four_states_with_zero_browser_calls(client, db, monkeypatch):
    monkeypatch.setattr(ninja_browser, "list_report_names", _explode)
    monkeypatch.setattr(ninja_browser, "search_report_names", _explode)
    monkeypatch.setattr(ninja_browser, "discover_report_names", _explode)

    ready_id = _create_client(db, slug="grp", name="GRP")
    ClientRepository(db).upsert_integration(
        ready_id, Provider.reporting_ninja, source_mode=SourceMode.api,
        config={"connection_key": "seo@x", "account_id": "properties/1",
                "ninja_report_name": "GRP-Internal report"},
    )
    _create_client(db, slug="saphny", name="SAPHNY")  # connected, no report name saved
    foreign_id = _create_client(db, slug="swm", name="SWM")
    ClientRepository(db).upsert_integration(
        foreign_id, Provider.reporting_ninja, source_mode=SourceMode.api,
        config={"connection_key": "seo@x", "account_id": "properties/1",
                "ninja_report_name": "EFFYS-Internal report"},
    )
    ClientRepository(db).create(
        name="ALPHA", slug="alpha", domain="alpha.example.com", timezone="America/New_York"
    )
    db.commit()

    body = client.get("/api/v1/reports/ninja-bindings").json()
    by_name = {row["name"]: row for row in body["rows"]}
    assert by_name["GRP"]["state"] == "ready"
    assert by_name["GRP"]["domain"] == "grp.example.com"
    assert by_name["SAPHNY"]["state"] == "no_report_name"
    assert by_name["SWM"]["state"] == "foreign_binding"
    assert by_name["ALPHA"]["state"] == "not_connected"


# --- POST /batches/{id}/clients/{id}/retry (task-49 §2.4) -------------------


def _create_batch(client, client_ids: list[str]) -> str:
    response = client.post(
        "/api/v1/reports/batches",
        json={"client_ids": client_ids, "kind": "monthly", "source": "api"},
    )
    assert response.status_code == 202
    return response.json()["batch_id"]


def test_retry_batch_client_stays_in_the_batch_and_the_batch_can_reach_succeeded(client, db):
    ok_id = _create_client(db, slug="ok", name="OK")
    fails_id = _create_client(db, slug="fails", name="FAILS")
    batch_id = _create_batch(client, [ok_id, fails_id])

    repo = ReportRepository(db)
    children = repo.list_runs_for_batch(batch_id)
    original_failed = next(c for c in children if c.client_id == fails_id)
    original_ok = next(c for c in children if c.client_id == ok_id)
    original_failed.status = "failed"
    original_ok.status = "succeeded"
    db.commit()

    response = client.post(
        f"/api/v1/reports/batches/{batch_id}/clients/{fails_id}/retry",
        json={"force_refresh": False},
    )
    assert response.status_code == 200
    body = response.json()
    assert len(body["clients"]) == 2  # never 3 — no orphan row for the old failed run
    retried_row = next(r for r in body["clients"] if r["client_id"] == fails_id)
    assert retried_row["run_id"] != original_failed.id
    assert retried_row["status"] == "queued"

    # ... and once the new child succeeds, the batch itself reaches succeeded.
    # No scheduler runs in these tests (module docstring) — `reconcile_batches`
    # is what the scheduler's own tick would normally call.
    with_new_run_id = retried_row["run_id"]
    new_run = ReportRepository(db).get(with_new_run_id)
    new_run.status = "succeeded"
    db.commit()
    reconcile_batches(worker_session_scope)
    final = client.get(f"/api/v1/reports/batches/{batch_id}").json()
    assert final["status"] == "succeeded"
    assert final["clients_done"] == 2
    assert final["clients_failed"] == 0


def test_retry_batch_client_leaves_every_other_client_untouched(client, db):
    # task-49 §4: "батчевий `Rebuild all` не чіпає клієнтів поза вибіркою" —
    # the bulk action is this endpoint applied per selected client, so the
    # guarantee that matters is that one client's retry never disturbs a
    # sibling's run.
    a_id = _create_client(db, slug="aaa", name="AAA")
    b_id = _create_client(db, slug="bbb", name="BBB")
    batch_id = _create_batch(client, [a_id, b_id])
    repo = ReportRepository(db)
    for child in repo.list_runs_for_batch(batch_id):
        child.status = "failed"
    db.commit()
    before = {
        row["client_id"]: row["run_id"]
        for row in client.get(f"/api/v1/reports/batches/{batch_id}").json()["clients"]
    }

    client.post(
        f"/api/v1/reports/batches/{batch_id}/clients/{a_id}/retry",
        json={"force_refresh": True},
    )

    after = {
        row["client_id"]: row["run_id"]
        for row in client.get(f"/api/v1/reports/batches/{batch_id}").json()["clients"]
    }
    assert after[a_id] != before[a_id], "the retried client got a fresh run"
    assert after[b_id] == before[b_id], "the client outside the selection was not touched"


def test_pdf_archive_says_so_instead_of_returning_an_empty_zip(client, db):
    # Live 2026-08-19: a 9-client batch downloaded a valid but EMPTY .zip
    # because no `client_pdf` had ever been rendered (see the ordering fix in
    # app/reports/runner.py). Empty-but-successful is the one answer this
    # endpoint must never give.
    client_id = _create_client(db)
    batch_id = _create_batch(client, [client_id])

    response = client.get(f"/api/v1/reports/batches/{batch_id}/pdf-archive")
    assert response.status_code == 404
    assert response.json()["detail"]["error_code"] == "no_client_pdfs"


def test_pdf_archive_404_for_an_unknown_batch_is_still_batch_not_found(client):
    response = client.get("/api/v1/reports/batches/does-not-exist/pdf-archive")
    assert response.status_code == 404
    assert response.json()["detail"]["error_code"] == "batch_not_found"


# --- GET /batches/{id}/portfolio.pdf, /batches/{id}/portfolio.xlsx (task-50) -


def _seed_client_pdf(db, client_id: str, run_id: str, name: str) -> None:
    from app.core.config import get_settings

    settings = get_settings()
    pdf_path = settings.artifacts_dir / "reports" / run_id / f"{name}.pdf"
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    pdf_path.write_bytes(b"%PDF-1.4 fake" + b"0" * 100)
    ReportRepository(db).create_file(
        client_id, run_id=run_id, kind="client_pdf",
        path=str(pdf_path.relative_to(settings.artifacts_dir)),
        sha256="x" * 64, size_bytes=pdf_path.stat().st_size, mime="application/pdf",
    )


def test_portfolio_pdf_merges_every_clients_pdf_alphabetically(client, db, monkeypatch):
    from app.api import reports as reports_api

    calls = []

    def fake_build(paths, out_path):
        calls.append(list(paths))
        out_path.write_bytes(b"%PDF-merged")
        return out_path

    monkeypatch.setattr(reports_api, "build_portfolio_pdf", fake_build)

    zzz_id = _create_client(db, slug="zzz", name="ZZZ")
    aaa_id = _create_client(db, slug="aaa", name="AAA")
    batch_id = _create_batch(client, [zzz_id, aaa_id])
    repo = ReportRepository(db)
    for child in repo.list_runs_for_batch(batch_id):
        name = "zzz" if child.client_id == zzz_id else "aaa"
        _seed_client_pdf(db, child.client_id, child.id, name)
    db.commit()

    response = client.get(f"/api/v1/reports/batches/{batch_id}/portfolio.pdf")
    assert response.status_code == 200
    assert response.content == b"%PDF-merged"
    assert "TheYovth_Portfolio" in response.headers["content-disposition"]
    [paths] = calls
    assert [p.name for p in paths] == ["aaa.pdf", "zzz.pdf"]  # alphabetical by CLIENT name


def test_portfolio_pdf_uses_only_the_retried_clients_newest_pdf(client, db, monkeypatch):
    # A retried client gets a brand-new `ReportRun` with the same `batch_id`
    # (task-49 §2.4) — the portfolio PDF must contribute that client's page
    # exactly once, from the newest run, never the stale one too.
    from app.api import reports as reports_api

    calls = []

    def fake_build(paths, out_path):
        calls.append(list(paths))
        out_path.write_bytes(b"x")
        return out_path

    monkeypatch.setattr(reports_api, "build_portfolio_pdf", fake_build)

    client_id = _create_client(db)
    batch_id = _create_batch(client, [client_id])
    repo = ReportRepository(db)
    [old_run] = repo.list_runs_for_batch(batch_id)
    old_run.status = "failed"  # a run must be terminal before it is retryable
    _seed_client_pdf(db, client_id, old_run.id, "old")
    db.commit()

    retry_response = client.post(
        f"/api/v1/reports/batches/{batch_id}/clients/{client_id}/retry",
        json={"force_refresh": False},
    )
    assert retry_response.status_code == 200
    [new_run] = [c for c in repo.list_runs_for_batch(batch_id) if c.id != old_run.id]
    _seed_client_pdf(db, client_id, new_run.id, "new")
    db.commit()

    client.get(f"/api/v1/reports/batches/{batch_id}/portfolio.pdf")
    [paths] = calls
    assert [p.name for p in paths] == ["new.pdf"]


def test_portfolio_pdf_404s_with_no_client_pdfs_instead_of_an_empty_file(client, db):
    client_id = _create_client(db)
    batch_id = _create_batch(client, [client_id])
    response = client.get(f"/api/v1/reports/batches/{batch_id}/portfolio.pdf")
    assert response.status_code == 404
    assert response.json()["detail"]["error_code"] == "no_client_pdfs"


def test_portfolio_pdf_404_for_an_unknown_batch(client):
    response = client.get("/api/v1/reports/batches/does-not-exist/portfolio.pdf")
    assert response.status_code == 404
    assert response.json()["detail"]["error_code"] == "batch_not_found"


def test_portfolio_pdf_inline_relaxes_frame_headers_for_the_preview_iframe(
    client, db, monkeypatch
):
    from app.api import reports as reports_api

    monkeypatch.setattr(
        reports_api, "build_portfolio_pdf",
        lambda paths, out_path: (out_path.write_bytes(b"%PDF-merged"), out_path)[-1],
    )
    client_id = _create_client(db)
    batch_id = _create_batch(client, [client_id])
    run = next(iter(ReportRepository(db).list_runs_for_batch(batch_id)))
    _seed_client_pdf(db, client_id, run.id, "one")
    db.commit()

    attachment = client.get(f"/api/v1/reports/batches/{batch_id}/portfolio.pdf")
    assert "attachment" in attachment.headers["content-disposition"]
    # The blanket `SecurityHeadersMiddleware` default (DENY) stands — only
    # the inline preview route relaxes it.
    assert attachment.headers.get("x-frame-options") == "DENY"

    inline = client.get(f"/api/v1/reports/batches/{batch_id}/portfolio.pdf?disposition=inline")
    assert inline.status_code == 200
    assert "inline" in inline.headers["content-disposition"]
    assert inline.headers["x-frame-options"] == "SAMEORIGIN"
    assert "frame-ancestors 'self'" in inline.headers["content-security-policy"]


def test_portfolio_xlsx_has_one_sheet_per_client_with_a_finished_result(client, db):
    from openpyxl import load_workbook

    with_result_id = _create_client(db, slug="ok", name="OK")
    still_running_id = _create_client(db, slug="pending", name="PENDING")
    batch_id = _create_batch(client, [with_result_id, still_running_id])
    repo = ReportRepository(db)
    for child in repo.list_runs_for_batch(batch_id):
        if child.client_id == with_result_id:
            repo.finish(
                child.id, status="succeeded",
                result={
                    "periods": [{
                        "label": "This Month", "date_from": "2026-07-01",
                        "date_to": "2026-07-31", "is_partial": False,
                    }],
                    "change_labels": [], "rows": [
                        {"key": "users", "label": "Users", "values": [10], "changes": []}
                    ],
                    "sources": {}, "history": None,
                },
            )
    db.commit()

    response = client.get(f"/api/v1/reports/batches/{batch_id}/portfolio.xlsx")
    assert response.status_code == 200
    import io as _io
    wb = load_workbook(_io.BytesIO(response.content))
    # PENDING never finished (`result_json` is still None) — it is skipped,
    # not shipped as a blank/broken sheet.
    assert wb.sheetnames == ["OK"]
    assert wb["OK"]["B3"].value == 10


def test_portfolio_xlsx_404s_when_nothing_has_finished_yet(client, db):
    client_id = _create_client(db)
    batch_id = _create_batch(client, [client_id])
    response = client.get(f"/api/v1/reports/batches/{batch_id}/portfolio.xlsx")
    assert response.status_code == 404
    assert response.json()["detail"]["error_code"] == "no_client_results"


def test_portfolio_xlsx_404_for_an_unknown_batch(client):
    response = client.get("/api/v1/reports/batches/does-not-exist/portfolio.xlsx")
    assert response.status_code == 404
    assert response.json()["detail"]["error_code"] == "batch_not_found"


def test_portfolio_table_returns_one_row_per_finished_client(client, db):
    with_result_id = _create_client(db, slug="ok", name="OK")
    still_running_id = _create_client(db, slug="pending", name="PENDING")
    batch_id = _create_batch(client, [with_result_id, still_running_id])
    repo = ReportRepository(db)
    for child in repo.list_runs_for_batch(batch_id):
        if child.client_id == with_result_id:
            repo.finish(
                child.id, status="succeeded",
                result={
                    "periods": [{
                        "label": "This Month", "date_from": "2026-07-01",
                        "date_to": "2026-07-31", "is_partial": False,
                    }],
                    "change_labels": [], "rows": [
                        {"key": "users", "label": "Users", "values": [10], "changes": []}
                    ],
                    "sources": {}, "history": None,
                },
            )
    db.commit()

    body = client.get(f"/api/v1/reports/batches/{batch_id}/portfolio-table").json()
    assert [row["client_name"] for row in body["clients"]] == ["OK"]
    assert body["clients"][0]["table"]["rows"][0]["values"] == [10]


def test_portfolio_table_is_an_empty_list_not_a_404_when_nothing_has_finished(client, db):
    client_id = _create_client(db)
    batch_id = _create_batch(client, [client_id])
    response = client.get(f"/api/v1/reports/batches/{batch_id}/portfolio-table")
    assert response.status_code == 200
    assert response.json()["clients"] == []


def test_portfolio_table_404_for_an_unknown_batch(client):
    response = client.get("/api/v1/reports/batches/does-not-exist/portfolio-table")
    assert response.status_code == 404
    assert response.json()["detail"]["error_code"] == "batch_not_found"


def test_retry_batch_client_404_for_unknown_batch(client, db):
    client_id = _create_client(db)
    response = client.post(
        f"/api/v1/reports/batches/does-not-exist/clients/{client_id}/retry",
        json={"force_refresh": False},
    )
    assert response.status_code == 404
    assert response.json()["detail"]["error_code"] == "batch_not_found"


def test_retry_batch_client_404_for_a_client_not_in_the_batch(client, db):
    in_batch_id = _create_client(db, slug="inbatch", name="InBatch")
    other_id = _create_client(db, slug="other", name="Other")
    batch_id = _create_batch(client, [in_batch_id])

    response = client.post(
        f"/api/v1/reports/batches/{batch_id}/clients/{other_id}/retry",
        json={"force_refresh": False},
    )
    assert response.status_code == 404
    assert response.json()["detail"]["error_code"] == "client_not_in_batch"


def test_retry_batch_client_409_while_still_running(client, db):
    client_id = _create_client(db)
    batch_id = _create_batch(client, [client_id])
    repo = ReportRepository(db)
    child = repo.list_runs_for_batch(batch_id)[0]
    child.status = "running"
    db.commit()

    response = client.post(
        f"/api/v1/reports/batches/{batch_id}/clients/{client_id}/retry",
        json={"force_refresh": True},
    )
    assert response.status_code == 409
    assert response.json()["detail"]["error_code"] == "client_not_retryable"


# --- POST /runs/{id}/rebuild (task-49 §2.5) ---------------------------------


def test_rebuild_run_creates_a_new_run_with_force_refresh_true(client, db):
    client_id = _create_client(db)
    run = ReportRepository(db).create_run(
        client_id, kind="monthly", source="pdf",
        params={"force_refresh": False}, period_count=6,
    )
    ReportRepository(db).finish(run.id, status="failed")
    db.commit()

    response = client.post(
        f"/api/v1/reports/runs/{run.id}/rebuild", json={"force_refresh": True}
    )
    assert response.status_code == 202
    new_run_id = response.json()["run_id"]
    assert new_run_id != run.id

    new_run = client.get(f"/api/v1/reports/runs/{new_run_id}").json()
    assert new_run["params"]["force_refresh"] is True
    assert new_run["status"] == "queued"
    assert new_run["batch_id"] is None  # this run was standalone


def test_rebuild_run_preserves_the_batch_link(client, db):
    client_id = _create_client(db)
    batch_id = _create_batch(client, [client_id])
    repo = ReportRepository(db)
    child = repo.list_runs_for_batch(batch_id)[0]
    child.status = "failed"
    db.commit()

    response = client.post(
        f"/api/v1/reports/runs/{child.id}/rebuild", json={"force_refresh": False}
    )
    assert response.status_code == 202
    new_run = client.get(f"/api/v1/reports/runs/{response.json()['run_id']}").json()
    assert new_run["batch_id"] == batch_id


def test_rebuild_run_404_for_unknown_run(client):
    response = client.post(
        "/api/v1/reports/runs/does-not-exist/rebuild", json={"force_refresh": True}
    )
    assert response.status_code == 404
    assert response.json()["detail"]["error_code"] == "run_not_found"


def test_rebuild_run_409_while_still_queued(client, db):
    client_id = _create_client(db)
    run = ReportRepository(db).create_run(
        client_id, kind="monthly", source="pdf", params={}, period_count=6,
    )
    db.commit()

    response = client.post(
        f"/api/v1/reports/runs/{run.id}/rebuild", json={"force_refresh": True}
    )
    assert response.status_code == 409
    assert response.json()["detail"]["error_code"] == "run_not_retryable"


# --- Authorship (task-93) -----------------------------------------------------
#
# `client`/`db` run with `AUTH_ENABLED=false` (see tests/conftest.py's
# `_auth_disabled_unless_asked`), so `Attribution.from_actor` sees `Actor.
# system()` there. Real-role coverage needs `auth_client` (task-71's own
# fixture) plus `auth_db` (`tests/unit/api/conftest.py`) — a sync session on
# ITS engine, not `client`'s. Every such test is `@pytest.mark.auth`
# (CLAUDE.md: the gate is off by default for the rest of this file,
# precisely so both fixtures can coexist here).


def test_create_run_records_system_attribution_when_auth_is_disabled(client, db):
    """Default dev/test posture (plan §2 D2): no guessed author, but not a
    silent `NULL` either — `Actor.system()` is an honest fact about who
    actually made this request."""
    client_id = _create_client(db, slug="task93-system")
    run_id = client.post(
        "/api/v1/reports/runs",
        json={"client_id": client_id, "kind": "monthly", "source": "api"},
    ).json()["run_id"]

    run = ReportRepository(db).get(run_id)
    assert run.created_by_kind == "system"
    assert run.created_by_user_id is None


@pytest.mark.auth
def test_create_run_records_the_real_signed_in_user(auth_client, auth_db, make_user, sign_in):
    client_id = _create_client(auth_db, slug="task93-member")
    email, password = make_user("member93a@theyovth.com", role="member")
    headers = sign_in(email, password)

    run_id = auth_client.post(
        "/api/v1/reports/runs",
        json={"client_id": client_id, "kind": "monthly", "source": "api"},
        headers=headers,
    ).json()["run_id"]

    run = ReportRepository(auth_db).get(run_id)
    user = UserRepository(auth_db).get_by_email(email)
    assert run.created_by_kind == "user"
    assert run.created_by_user_id == user.id


@pytest.mark.auth
def test_rebuild_run_is_attributed_to_whoever_clicked_rebuild(
    auth_client, auth_db, make_user, sign_in
):
    """task-93 plan §2 D5: a rebuild's author is whoever clicked it NOW —
    never the source run's original author."""
    # `sign_in` logs into the ONE shared `auth_client` cookie jar — signing
    # in as the second user before the first has finished acting would swap
    # the active session out from under it, so each identity signs in and
    # acts immediately, never both live at once.
    client_id = _create_client(auth_db, slug="task93-rebuild")
    author_email, author_password = make_user("author93@theyovth.com", role="member")
    author_headers = sign_in(author_email, author_password)
    run_id = auth_client.post(
        "/api/v1/reports/runs",
        json={"client_id": client_id, "kind": "monthly", "source": "api"},
        headers=author_headers,
    ).json()["run_id"]
    # No scheduler runs in this test file — flip the run terminal by hand,
    # same convention as `test_rebuild_run_409_while_still_queued` above.
    ReportRepository(auth_db).get(run_id).status = "succeeded"
    auth_db.commit()

    rebuilder_email, rebuilder_password = make_user("rebuilder93@theyovth.com", role="admin")
    rebuilder_headers = sign_in(rebuilder_email, rebuilder_password)
    rebuild = auth_client.post(
        f"/api/v1/reports/runs/{run_id}/rebuild",
        json={"force_refresh": False},
        headers=rebuilder_headers,
    )
    assert rebuild.status_code == 202
    new_run_id = rebuild.json()["run_id"]

    new_run = ReportRepository(auth_db).get(new_run_id)
    author = UserRepository(auth_db).get_by_email(author_email)
    rebuilder = UserRepository(auth_db).get_by_email(rebuilder_email)
    assert new_run.created_by_user_id == rebuilder.id
    assert new_run.created_by_user_id != author.id


@pytest.mark.auth
def test_created_by_filter_is_admin_only_and_lists_stay_role_invariant_without_it(
    auth_client, auth_db, make_user, sign_in
):
    """task-93 plan §2 D4: a filter over who ran what is an oracle, so even
    SETTING it as a non-admin is `403`, never a silently-ignored param — and
    the plain list (no filter) must be byte-identical for every role, since
    that's the only thing safe to cache (plan §1.5's Cloudflare tunnel
    incident: a role-conditional list body could leak through a cached
    response)."""
    # Same one-identity-live-at-a-time constraint as the rebuild test above:
    # `admin_plain` is captured BEFORE switching the shared cookie jar to
    # member, not re-fetched afterwards under the wrong session.
    client_id = _create_client(auth_db, slug="task93-filter")
    admin_email, admin_password = make_user("admin93a@theyovth.com", role="admin")
    admin_headers = sign_in(admin_email, admin_password)

    auth_client.post(
        "/api/v1/reports/runs",
        json={"client_id": client_id, "kind": "monthly", "source": "api"},
        headers=admin_headers,
    )

    admin_filtered = auth_client.get(
        "/api/v1/reports/runs?created_by_kind=user", headers=admin_headers
    )
    assert admin_filtered.status_code == 200
    assert admin_filtered.json()["total"] == 1
    admin_plain = auth_client.get("/api/v1/reports/runs", headers=admin_headers).json()

    member_email, member_password = make_user("member93c@theyovth.com", role="member")
    member_headers = sign_in(member_email, member_password)

    member_filtered_by_kind = auth_client.get(
        "/api/v1/reports/runs?created_by_kind=user", headers=member_headers
    )
    assert member_filtered_by_kind.status_code == 403

    member_filtered_by_user = auth_client.get(
        f"/api/v1/reports/runs?created_by_user_id={admin_email}", headers=member_headers
    )
    assert member_filtered_by_user.status_code == 403

    member_plain = auth_client.get("/api/v1/reports/runs", headers=member_headers).json()
    assert admin_plain == member_plain
    assert "created_by" not in str(admin_plain)  # no field leaked onto the shared DTO
