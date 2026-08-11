"""Site Monitor REST API (task-35 §3.8/§5): each endpoint's happy path,
"client doesn't exist" (404), "no runs yet" (an honest empty state, never a
500), a version diff, and ack.
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.db.repositories.clients import ClientRepository
from app.db.repositories.monitor import MonitorRepository


def _create_client(db, *, slug: str = "acme") -> str:
    client = ClientRepository(db).create(
        name="Acme", slug=slug, domain=f"{slug}.com", timezone="America/New_York"
    )
    db.commit()
    return client.id


# --- summary -----------------------------------------------------------------


def test_monitor_summary_404_for_unknown_client(client):
    response = client.get("/api/v1/clients/nope/monitor")
    assert response.status_code == 404
    assert response.json()["detail"]["error_code"] == "client_not_found"


def test_monitor_summary_is_an_honest_empty_state_before_any_run(client, db):
    client_id = _create_client(db)
    response = client.get(f"/api/v1/clients/{client_id}/monitor")
    assert response.status_code == 200
    body = response.json()
    assert body["last_light_run"] is None
    assert body["last_full_run"] is None
    assert body["open_critical"] == 0
    assert body["sitemap_health"] == {
        "total": 0,
        "ok": 0,
        "redirects": 0,
        "broken": 0,
        "unavailable": 0,
    }


def test_monitor_summary_reflects_a_finished_run_and_open_changes(client, db):
    client_id = _create_client(db)
    repo = MonitorRepository(db)
    run = repo.create_run(client_id, kind="full")
    repo.finish_run(run.id, status="succeeded")
    repo.record_change(
        client_id,
        run_id=run.id,
        severity="critical",
        change_type="page_noindex_appeared",
        scope="page",
        subject="https://acme.com/",
    )
    db.commit()

    body = client.get(f"/api/v1/clients/{client_id}/monitor").json()
    assert body["last_full_run"]["status"] == "succeeded"
    assert body["open_critical"] == 1


# --- runs ----------------------------------------------------------------------


def test_list_runs_404_for_unknown_client(client):
    assert client.get("/api/v1/clients/nope/monitor/runs").status_code == 404


def test_list_runs_empty_before_any_run(client, db):
    client_id = _create_client(db)
    response = client.get(f"/api/v1/clients/{client_id}/monitor/runs")
    assert response.status_code == 200
    assert response.json() == []


def test_list_runs_filters_by_kind(client, db):
    client_id = _create_client(db)
    repo = MonitorRepository(db)
    repo.finish_run(repo.create_run(client_id, kind="light").id, status="succeeded")
    repo.finish_run(repo.create_run(client_id, kind="full").id, status="succeeded")
    db.commit()

    only_full = client.get(f"/api/v1/clients/{client_id}/monitor/runs?kind=full").json()
    assert len(only_full) == 1
    assert only_full[0]["kind"] == "full"


def test_start_run_404_for_unknown_client(client):
    response = client.post("/api/v1/clients/nope/monitor/runs")
    assert response.status_code == 404


def test_start_run_enqueues_without_running_it_in_the_api_process(client, db):
    client_id = _create_client(db)
    response = client.post(f"/api/v1/clients/{client_id}/monitor/runs?kind=full")
    assert response.status_code == 202
    run_id = response.json()["run_id"]

    run = MonitorRepository(db).get_run(run_id)
    # Enqueued, not executed: the API process never crawls a site itself
    # (task-35 §3.8) — the worker's scheduler is the sole claimer.
    assert run.status == "queued"
    assert run.kind == "full"
    assert run.trigger == "manual"


# --- changes -------------------------------------------------------------------


def test_client_changes_404_for_unknown_client(client):
    assert client.get("/api/v1/clients/nope/monitor/changes").status_code == 404


def test_client_changes_default_state_is_open_only(client, db):
    client_id = _create_client(db)
    repo = MonitorRepository(db)
    run = repo.create_run(client_id, kind="full")
    open_change = repo.record_change(
        client_id,
        run_id=run.id,
        severity="warning",
        change_type="title_changed",
        scope="page",
        subject="https://acme.com/",
    )
    resolved = repo.record_change(
        client_id,
        run_id=run.id,
        severity="warning",
        change_type="h1_changed",
        scope="page",
        subject="https://acme.com/about/",
    )
    repo.resolve_change(resolved)
    db.commit()

    open_only = client.get(f"/api/v1/clients/{client_id}/monitor/changes").json()
    assert [c["id"] for c in open_only] == [open_change.id]

    everything = client.get(f"/api/v1/clients/{client_id}/monitor/changes?state=all").json()
    assert len(everything) == 2


def test_portfolio_changes_spans_clients(client, db):
    client_a = _create_client(db, slug="a")
    client_b = _create_client(db, slug="b")
    repo = MonitorRepository(db)
    run_a = repo.create_run(client_a, kind="full")
    run_b = repo.create_run(client_b, kind="full")
    repo.record_change(
        client_a,
        run_id=run_a.id,
        severity="critical",
        change_type="site_down",
        scope="site",
        subject="a.com",
    )
    repo.record_change(
        client_b,
        run_id=run_b.id,
        severity="warning",
        change_type="title_changed",
        scope="page",
        subject="https://b.com/",
    )
    db.commit()

    critical_only = client.get("/api/v1/monitor/changes?severity=critical").json()
    assert len(critical_only) == 1
    assert critical_only[0]["client_id"] == client_a


def test_ack_change_404_for_unknown_change(client):
    response = client.post("/api/v1/monitor/changes/nope/ack", json={"note": "seen"})
    assert response.status_code == 404


def test_ack_change_records_note_and_timestamp(client, db):
    client_id = _create_client(db)
    repo = MonitorRepository(db)
    run = repo.create_run(client_id, kind="full")
    change = repo.record_change(
        client_id,
        run_id=run.id,
        severity="warning",
        change_type="title_changed",
        scope="page",
        subject="https://acme.com/",
    )
    db.commit()

    response = client.post(f"/api/v1/monitor/changes/{change.id}/ack", json={"note": "known issue"})
    assert response.status_code == 200
    body = response.json()
    assert body["acknowledged_note"] == "known issue"
    assert body["acknowledged_at"] is not None


def test_ack_rejects_unknown_fields(client, db):
    client_id = _create_client(db)
    repo = MonitorRepository(db)
    run = repo.create_run(client_id, kind="full")
    change = repo.record_change(
        client_id,
        run_id=run.id,
        severity="info",
        change_type="page_new",
        scope="page",
        subject="https://acme.com/",
    )
    db.commit()

    response = client.post(
        f"/api/v1/monitor/changes/{change.id}/ack", json={"note": "x", "severity": "critical"}
    )
    assert response.status_code == 422


# --- pages -----------------------------------------------------------------


def test_list_pages_404_for_unknown_client(client):
    assert client.get("/api/v1/clients/nope/monitor/pages").status_code == 404


def test_list_pages_empty_before_any_run(client, db):
    client_id = _create_client(db)
    body = client.get(f"/api/v1/clients/{client_id}/monitor/pages").json()
    assert body == {"pages": [], "total": 0, "limit": 100, "offset": 0}


def test_list_pages_status_filter(client, db):
    client_id = _create_client(db)
    repo = MonitorRepository(db)
    ok_page = repo.get_or_create_page(client_id, "https://acme.com/ok/")
    repo.upsert_page_state(
        ok_page,
        status=200,
        final_url=ok_page.url,
        hop_count=0,
        indexable=True,
        meta_robots=None,
        x_robots_tag=None,
        canonical=None,
        title=None,
        description=None,
        h1=None,
        word_count=1,
        analytics_ids=[],
        signals_sha256="s",
        text_sha256="t",
        raw_sha256="r",
        in_sitemap=True,
        now=datetime.now(UTC),
    )
    broken_page = repo.get_or_create_page(client_id, "https://acme.com/broken/")
    repo.upsert_page_state(
        broken_page,
        status=404,
        final_url=broken_page.url,
        hop_count=0,
        indexable=False,
        meta_robots=None,
        x_robots_tag=None,
        canonical=None,
        title=None,
        description=None,
        h1=None,
        word_count=1,
        analytics_ids=[],
        signals_sha256="s2",
        text_sha256="t2",
        raw_sha256="r2",
        in_sitemap=True,
        now=datetime.now(UTC),
    )
    db.commit()

    broken_only = client.get(f"/api/v1/clients/{client_id}/monitor/pages?status=broken").json()
    assert [p["url"] for p in broken_only["pages"]] == [broken_page.url]


def test_page_versions_404_for_unknown_page(client, db):
    client_id = _create_client(db)
    response = client.get(f"/api/v1/clients/{client_id}/monitor/pages/nope/versions")
    assert response.status_code == 404


def test_page_diff_between_two_versions(client, db):
    client_id = _create_client(db)
    repo = MonitorRepository(db)
    page = repo.get_or_create_page(client_id, "https://acme.com/")
    v1 = repo.create_page_version(
        page,
        http_status=200,
        signals={"title": "Old"},
        signals_sha256="s1",
        text_sha256="t1",
        raw_sha256="r1",
        body_blob_sha256=None,
        changed_fields=[],
        prev_version_id=None,
    )
    v2 = repo.create_page_version(
        page,
        http_status=200,
        signals={"title": "New"},
        signals_sha256="s2",
        text_sha256="t2",
        raw_sha256="r2",
        body_blob_sha256=None,
        changed_fields=["title"],
        prev_version_id=v1.id,
    )
    db.commit()

    response = client.get(f"/api/v1/clients/{client_id}/monitor/pages/{page.id}/diff")
    assert response.status_code == 200
    body = response.json()
    assert body["from_version_id"] == v1.id
    assert body["to_version_id"] == v2.id
    assert body["changed_fields"] == ["title"]


def test_page_diff_404_when_fewer_than_two_versions_exist(client, db):
    client_id = _create_client(db)
    repo = MonitorRepository(db)
    page = repo.get_or_create_page(client_id, "https://acme.com/")
    repo.create_page_version(
        page,
        http_status=200,
        signals={},
        signals_sha256="s1",
        text_sha256="t1",
        raw_sha256="r1",
        body_blob_sha256=None,
        changed_fields=[],
        prev_version_id=None,
    )
    db.commit()

    response = client.get(f"/api/v1/clients/{client_id}/monitor/pages/{page.id}/diff")
    assert response.status_code == 404
    assert response.json()["detail"]["error_code"] == "version_not_found"


# --- resources (robots/sitemap) -----------------------------------------------


def test_resource_versions_404_when_never_checked(client, db):
    client_id = _create_client(db)
    response = client.get(f"/api/v1/clients/{client_id}/monitor/resources/robots/versions")
    assert response.status_code == 404


def test_resource_diff_reads_changed_fields(client, db):
    client_id = _create_client(db)
    repo = MonitorRepository(db)
    resource = repo.upsert_resource_state(
        client_id,
        "https://acme.com/robots.txt",
        kind="robots",
        status=200,
        content_sha256="a",
        structure={"groups": []},
        missing=False,
    )
    v1 = repo.create_resource_version(
        resource,
        http_status=200,
        content_sha256="a",
        structure={"groups": []},
        changed_fields=[],
        prev_version_id=None,
    )
    v2 = repo.create_resource_version(
        resource,
        http_status=200,
        content_sha256="b",
        structure={"groups": ["*"]},
        changed_fields=["group:*"],
        prev_version_id=v1.id,
    )
    db.commit()

    response = client.get(f"/api/v1/clients/{client_id}/monitor/resources/robots/diff")
    assert response.status_code == 200
    body = response.json()
    assert body["to_version_id"] == v2.id
    assert body["changed_fields"] == ["group:*"]


# --- DNS -----------------------------------------------------------------------


def test_dns_404_for_unknown_client(client):
    assert client.get("/api/v1/clients/nope/monitor/dns").status_code == 404


def test_dns_records_round_trip(client, db):
    client_id = _create_client(db)
    repo = MonitorRepository(db)
    repo.upsert_dns_state(
        client_id, "acme.com", "A", values=["1.2.3.4"], ttl=300, resolver="dns.google"
    )
    db.commit()

    body = client.get(f"/api/v1/clients/{client_id}/monitor/dns").json()
    assert body[0]["values"] == ["1.2.3.4"]

    versions = client.get(
        f"/api/v1/clients/{client_id}/monitor/dns/versions?name=acme.com&record_type=A"
    ).json()
    assert versions == []  # state written directly, no version row created in this test


# --- settings --------------------------------------------------------------


def test_get_settings_404_for_unknown_client(client):
    assert client.get("/api/v1/clients/nope/monitor/settings").status_code == 404


def test_get_settings_defaults(client, db):
    client_id = _create_client(db)
    body = client.get(f"/api/v1/clients/{client_id}/monitor/settings").json()
    assert body["enabled"] is True
    assert body["monitor_when_paused"] is False


def test_patch_settings_updates_only_sent_fields(client, db):
    client_id = _create_client(db)
    client.patch(f"/api/v1/clients/{client_id}/monitor/settings", json={"enabled": False})
    body = client.get(f"/api/v1/clients/{client_id}/monitor/settings").json()
    assert body["enabled"] is False
    assert body["monitor_when_paused"] is False


def test_patch_settings_404_for_unknown_client(client):
    response = client.patch("/api/v1/clients/nope/monitor/settings", json={"enabled": False})
    assert response.status_code == 404


def test_patch_settings_rejects_unknown_fields(client, db):
    client_id = _create_client(db)
    response = client.patch(
        f"/api/v1/clients/{client_id}/monitor/settings", json={"not_a_real_field": 1}
    )
    assert response.status_code == 422


# --- documents / export -------------------------------------------------------


def test_document_404_for_unknown_sha256(client):
    response = client.get("/api/v1/monitor/documents/deadbeef")
    assert response.status_code == 404


def test_export_csv_404_for_unknown_client(client):
    assert client.get("/api/v1/clients/nope/monitor/export.csv").status_code == 404


def test_export_csv_for_a_client_with_no_pages_yet(client, db):
    client_id = _create_client(db)
    response = client.get(f"/api/v1/clients/{client_id}/monitor/export.csv")
    assert response.status_code == 200
    assert "text/csv" in response.headers["content-type"]


def test_export_xlsx_for_a_client_with_no_pages_yet(client, db):
    client_id = _create_client(db)
    response = client.get(f"/api/v1/clients/{client_id}/monitor/export.xlsx")
    assert response.status_code == 200
