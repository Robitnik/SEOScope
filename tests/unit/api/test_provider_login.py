"""task-22: HTTP surface for task-20's `check_session`/`start_login` — neither
task-20 nor task-21 put a route on these, so the sync modal's «Увійти»
button had nothing to call without this file's two routes.
"""

from __future__ import annotations

import app.api.providers as providers_module
from app.integrations.browser_session import NoWarmChrome, SessionState
from app.jobs.lane import DEFAULT_AUTH_PAUSE_TIMEOUT_SECONDS
from app.schemas.enums import Provider


def test_login_rejects_provider_with_no_browser_session(client):
    response = client.post("/api/v1/providers/reporting_ninja/login")
    assert response.status_code == 400
    assert response.json()["detail"]["error_code"] == "provider_has_no_session"


def test_login_reports_no_warm_chrome_with_exact_remedy(client, monkeypatch):
    def _raise(_provider: Provider) -> str:
        raise NoWarmChrome(Provider.semrush, "seo-audit browser-up --provider semrush")

    monkeypatch.setattr(providers_module, "start_login", _raise)

    response = client.post("/api/v1/providers/semrush/login")
    assert response.status_code == 503
    body = response.json()["detail"]
    assert body["error_code"] == "no_warm_chrome"
    assert "seo-audit browser-up --provider semrush" in body["message"]


def test_login_happy_path_returns_session_id_state_and_timeout(client, monkeypatch):
    monkeypatch.setattr(providers_module, "start_login", lambda _p: "login-abc123")
    monkeypatch.setattr(providers_module, "check_session", lambda _p: SessionState.expired)

    response = client.post("/api/v1/providers/semrush/login")
    assert response.status_code == 200
    body = response.json()
    assert body["login_session_id"] == "login-abc123"
    assert body["state"] == "expired"
    assert body["timeout_seconds"] == int(DEFAULT_AUTH_PAUSE_TIMEOUT_SECONDS)


def test_session_endpoint_reflects_check_session(client, monkeypatch):
    monkeypatch.setattr(providers_module, "check_session", lambda _p: SessionState.alive)

    response = client.get("/api/v1/providers/ahrefs/session")
    assert response.status_code == 200
    assert response.json() == {"state": "alive"}


def test_session_endpoint_rejects_provider_with_no_browser_session(client):
    response = client.get("/api/v1/providers/reporting_ninja/session")
    assert response.status_code == 400
    assert response.json()["detail"]["error_code"] == "provider_has_no_session"
