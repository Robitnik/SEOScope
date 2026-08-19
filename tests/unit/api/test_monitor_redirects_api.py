"""Redirect monitoring REST API (task-51 §3.8): list/status/check, each
endpoint's "client doesn't exist" (404), and an honest empty state before
any collection has ever run (never a 500).
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.db.repositories.clients import ClientRepository
from app.db.repositories.monitor import MonitorRepository
from app.monitor.redirects import collect as collect_mod
from app.monitor.redirects.contract import RedirectRule, RedirectSourceResult


def _create_client(db, *, slug: str = "acme") -> str:
    client = ClientRepository(db).create(
        name="Acme", slug=slug, domain=f"{slug}.com", timezone="America/New_York"
    )
    db.commit()
    return client.id


def _rule(key: str, **overrides) -> RedirectRule:
    base = dict(
        rule_key=key,
        source_url=f"/old-{key}/",
        target_url=f"/new-{key}/",
        http_code=301,
        match_type="url",
        regex=False,
        status="enabled",
        group_name="Redirections",
        title=None,
        external_id=int(key),
    )
    base.update(overrides)
    return RedirectRule(**base)


# --- list ----------------------------------------------------------------------


def test_list_redirects_404_for_unknown_client(client):
    response = client.get("/api/v1/clients/nope/monitor/redirects")
    assert response.status_code == 404
    assert response.json()["detail"]["error_code"] == "client_not_found"


def test_list_redirects_is_an_honest_empty_state_before_any_collection(client, db):
    client_id = _create_client(db)
    response = client.get(f"/api/v1/clients/{client_id}/monitor/redirects")
    assert response.status_code == 200
    body = response.json()
    assert body == {"redirects": [], "total": 0, "limit": body["limit"], "offset": 0}


def test_list_redirects_returns_persisted_rules_with_window_fields(client, db):
    client_id = _create_client(db)
    repo = MonitorRepository(db)
    repo.upsert_redirect_state(
        client_id,
        "redirection",
        "1",
        source_url="/old/",
        target_url="/new/",
        http_code=301,
        match_type="url",
        regex=False,
        status="enabled",
        group_name="Modified Posts",
        title=None,
        external_id=1,
        rule_sha256="a" * 64,
        now=datetime(2026, 8, 19, 14, 0, tzinfo=UTC),
        window_start=datetime(2026, 8, 19, 13, 0, tzinfo=UTC),
    )
    db.commit()

    response = client.get(f"/api/v1/clients/{client_id}/monitor/redirects")
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    row = body["redirects"][0]
    assert row["source_url"] == "/old/"
    assert row["group_name"] == "Modified Posts"
    assert row["first_seen_window_start"] == "2026-08-19T13:00:00Z"
    assert row["first_seen_at"] == "2026-08-19T14:00:00Z"
    assert row["removed_at"] is None


def test_list_redirects_state_filter_active_vs_removed(client, db):
    client_id = _create_client(db)
    repo = MonitorRepository(db)
    for key in ("1", "2"):
        repo.upsert_redirect_state(
            client_id,
            "redirection",
            key,
            source_url=f"/old-{key}/",
            target_url=f"/new-{key}/",
            http_code=301,
            match_type="url",
            regex=False,
            status="enabled",
            group_name=None,
            title=None,
            external_id=int(key),
            rule_sha256="a" * 64,
        )
    repo.mark_redirects_removed(client_id, "redirection", {"1"})
    db.commit()

    active = client.get(f"/api/v1/clients/{client_id}/monitor/redirects?state=active").json()
    removed = client.get(f"/api/v1/clients/{client_id}/monitor/redirects?state=removed").json()
    assert [r["source_url"] for r in active["redirects"]] == ["/old-1/"]
    assert [r["source_url"] for r in removed["redirects"]] == ["/old-2/"]


def test_list_redirects_search_filters_by_source_url(client, db):
    client_id = _create_client(db)
    repo = MonitorRepository(db)
    repo.upsert_redirect_state(
        client_id,
        "redirection",
        "1",
        source_url="/special-landing/",
        target_url="/x/",
        http_code=301,
        match_type="url",
        regex=False,
        status="enabled",
        group_name=None,
        title=None,
        external_id=1,
        rule_sha256="a" * 64,
    )
    repo.upsert_redirect_state(
        client_id,
        "redirection",
        "2",
        source_url="/other/",
        target_url="/y/",
        http_code=301,
        match_type="url",
        regex=False,
        status="enabled",
        group_name=None,
        title=None,
        external_id=2,
        rule_sha256="b" * 64,
    )
    db.commit()

    response = client.get(f"/api/v1/clients/{client_id}/monitor/redirects?q=special")
    body = response.json()
    assert body["total"] == 1
    assert body["redirects"][0]["source_url"] == "/special-landing/"


# --- status ----------------------------------------------------------------------


def test_redirects_status_404_for_unknown_client(client):
    response = client.get("/api/v1/clients/nope/monitor/redirects/status")
    assert response.status_code == 404


def test_redirects_status_before_any_collection_is_honest_not_configured(client, db):
    client_id = _create_client(db)
    response = client.get(f"/api/v1/clients/{client_id}/monitor/redirects/status")
    assert response.status_code == 200
    body = response.json()
    assert body["redirect_source"] == "auto"
    assert body["credentials_status"] == "missing"
    assert body["last_checked_at"] is None
    assert body["rules_total"] == 0


def test_redirects_status_configured_via_login_credentials_alone(client, db, monkeypatch, tmp_path):
    """Application Password absent, only a regular wp-admin login on file —

    task-51 follow-up (2026-08-19 live finding: this is the mechanism that
    actually works today) — must still report `configured`, not `missing`.

    Points `WP_LOGIN_CREDENTIALS_PATH` at a throwaway file (same env-var +
    cache-clear convention this file's own `client` fixture uses for
    `DATABASE_URL`/`ARTIFACTS_DIR`) rather than writing into this machine's
    real `app_data/secrets/` — that file holds actual client credentials.
    """
    from app.core.config import get_settings
    from app.monitor.redirects.credentials import write_login_credentials

    client_id = _create_client(db)
    monkeypatch.setenv("WP_LOGIN_CREDENTIALS_PATH", str(tmp_path / "wp_login_credentials.json"))
    get_settings.cache_clear()
    settings = get_settings()
    write_login_credentials(
        settings.wp_login_credentials_path, client_id, login="admin", password="pw"
    )
    body = client.get(f"/api/v1/clients/{client_id}/monitor/redirects/status").json()
    assert body["credentials_status"] == "configured"
    get_settings.cache_clear()


def test_redirects_status_reports_rule_counts(client, db):
    client_id = _create_client(db)
    repo = MonitorRepository(db)
    repo.update_settings(client_id, redirect_source="redirection")
    repo.upsert_redirect_state(
        client_id,
        "redirection",
        "1",
        source_url="/a/",
        target_url="/b/",
        http_code=301,
        match_type="url",
        regex=False,
        status="enabled",
        group_name=None,
        title=None,
        external_id=1,
        rule_sha256="a" * 64,
    )
    repo.upsert_redirect_state(
        client_id,
        "redirection",
        "2",
        source_url="/c/",
        target_url="/d/",
        http_code=301,
        match_type="url",
        regex=False,
        status="disabled",
        group_name=None,
        title=None,
        external_id=2,
        rule_sha256="b" * 64,
    )
    db.commit()

    body = client.get(f"/api/v1/clients/{client_id}/monitor/redirects/status").json()
    assert body["rules_total"] == 2
    assert body["rules_enabled"] == 1
    assert body["rules_disabled"] == 1


# --- check -------------------------------------------------------------------


def test_check_redirects_404_for_unknown_client(client):
    response = client.post("/api/v1/clients/nope/monitor/redirects/check")
    assert response.status_code == 404


def test_check_redirects_runs_synchronously_and_persists_rules(client, db, monkeypatch):
    client_id = _create_client(db)

    monkeypatch.setattr(
        collect_mod,
        "_fetch_snapshot",
        lambda *a, **k: RedirectSourceResult(
            ok=True, source="redirection", rules=(_rule("1"), _rule("2"))
        ),
    )

    response = client.post(f"/api/v1/clients/{client_id}/monitor/redirects/check")
    assert response.status_code == 200
    body = response.json()
    assert body["rules_total"] == 2
    assert body["last_checked_at"] is not None

    listed = client.get(f"/api/v1/clients/{client_id}/monitor/redirects").json()
    assert listed["total"] == 2


def test_check_redirects_writes_a_run_row(client, db, monkeypatch):
    client_id = _create_client(db)
    monkeypatch.setattr(
        collect_mod,
        "_fetch_snapshot",
        lambda *a, **k: RedirectSourceResult(ok=True, source="redirection", rules=()),
    )
    client.post(f"/api/v1/clients/{client_id}/monitor/redirects/check")

    runs = client.get(f"/api/v1/clients/{client_id}/monitor/runs").json()
    assert len(runs) == 1
    assert runs[0]["trigger"] == "manual"
    assert runs[0]["status"] == "succeeded"


def test_check_redirects_surfaces_unavailable_reason_in_status(client, db, monkeypatch):
    client_id = _create_client(db)
    monkeypatch.setattr(
        collect_mod,
        "_fetch_snapshot",
        lambda *a, **k: RedirectSourceResult(
            ok=False, source="redirection", unavailable_reason="auth_failed"
        ),
    )
    body = client.post(f"/api/v1/clients/{client_id}/monitor/redirects/check").json()
    assert body["last_unavailable_reason"] == "auth_failed"
