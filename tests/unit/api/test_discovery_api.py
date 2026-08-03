"""Юніти ендпоінта автопідключення (task-33).

Головне, що тут перевіряється, — коли ендпоінт **пише** в БД, а коли ні, і чи
переживає запис уже наявний операторський конфіг.
"""

from __future__ import annotations

import pytest

from app.db.repositories.clients import ClientRepository
from app.integrations import discovery, registry
from app.schemas.adapter import PreflightResult
from app.schemas.enums import Provider, SourceMode

_URL = "/api/v1/clients/{client_id}/integrations/{provider}/discover"


@pytest.fixture(autouse=True)
def _clear_cache():
    discovery.reset_cache()
    yield
    discovery.reset_cache()


def _seed_client(db, **overrides):
    fields = {
        "name": "LAXTON",
        "slug": "laxton",
        "domain": "laxton.se",
        "market": "SE",
    } | overrides
    created = ClientRepository(db).create(**fields)
    db.commit()
    return created.id


def _patch_ninja(monkeypatch, *accounts: tuple[str, str]):
    payload = [
        {
            "connection_key": "seo@example.com",
            "accounts": [
                {"account_id": account_id, "account_name": name} for account_id, name in accounts
            ],
        }
    ]
    monkeypatch.setattr(
        discovery, "list_ninja_accounts", lambda _id, **_kw: payload
    )


class _OkAdapter:
    """Мінімальний адаптер, щоб пройти preflight після запису."""

    def __init__(self, provider: Provider, ok: bool = True) -> None:
        self.provider = provider
        self._ok = ok

    def preflight(self, context):
        return PreflightResult(provider=self.provider, ok=self._ok)

    def collect(self, context, emit, is_cancel_requested):  # pragma: no cover - не викликається
        raise NotImplementedError


def test_unknown_client_is_404(client):
    response = client.post(_URL.format(client_id="nope", provider="gsc"), json={})

    assert response.status_code == 404
    assert response.json()["detail"]["error_code"] == "client_not_found"


def test_machine_only_provider_is_rejected(client, db):
    """`tech_audit`/`blogs` не мають акаунта, який можна знайти."""
    client_id = _seed_client(db)

    response = client.post(_URL.format(client_id=client_id, provider="tech_audit"), json={})

    assert response.status_code == 400
    assert response.json()["detail"]["error_code"] == "provider_not_connectable"


def test_gsc_is_connected_and_verified(client, db, monkeypatch):
    client_id = _seed_client(db)
    _patch_ninja(monkeypatch, ("sc-domain:laxton.se", "sc-domain:laxton.se"))
    registry.register_adapter(_OkAdapter(Provider.gsc))

    response = client.post(_URL.format(client_id=client_id, provider="gsc"), json={})

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "connected"
    assert body["preflight"]["ok"] is True
    assert body["applied"]["source_mode"] == "api"
    assert body["applied"]["integration_config"]["account_id"] == "sc-domain:laxton.se"

    stored = ClientRepository(db).get_integration(client_id, Provider.gsc)
    assert stored is not None
    assert stored.enabled is True
    assert stored.config_json["property"] == "sc-domain:laxton.se"


def test_failed_preflight_is_reported_without_hiding_the_write(client, db, monkeypatch):
    """Запис лишається, але звіт має чесно сказати, що верифікація не пройшла."""
    client_id = _seed_client(db)
    _patch_ninja(monkeypatch, ("sc-domain:laxton.se", "sc-domain:laxton.se"))
    registry.register_adapter(_OkAdapter(Provider.gsc, ok=False))

    body = client.post(_URL.format(client_id=client_id, provider="gsc"), json={}).json()

    assert body["status"] == "connected"
    assert body["preflight"]["ok"] is False
    assert "Verification failed" in body["message"]


def test_auto_apply_merges_and_never_clobbers_operator_keys(client, db, monkeypatch):
    """`upsert_integration` затирає `config_json` цілком — merge мусить бути тут.

    `unit_budget` — свідома операторська політика (дозвіл на платний API-fallback
    Semrush). Автопідключення, яке її мовчки знімає, змінює вартість збору.
    """
    client_id = _seed_client(db)
    repo = ClientRepository(db)
    repo.upsert_integration(
        client_id,
        Provider.semrush,
        enabled=False,
        config={"unit_budget": 500, "csv_dir": "/data/semrush"},
    )
    db.commit()
    registry.register_adapter(_OkAdapter(Provider.semrush))

    body = client.post(_URL.format(client_id=client_id, provider="semrush"), json={}).json()

    assert body["status"] == "connected"
    stored = ClientRepository(db).get_integration(client_id, Provider.semrush)
    assert stored is not None
    assert stored.config_json["unit_budget"] == 500
    assert stored.config_json["csv_dir"] == "/data/semrush"
    assert stored.config_json["domain"] == "laxton.se"
    assert stored.config_json["database"] == "se"
    assert stored.enabled is True


def test_ambiguous_match_writes_nothing(client, db, monkeypatch):
    client_id = _seed_client(db)
    _patch_ninja(
        monkeypatch,
        ("properties/1", "Laxton - GA4"),
        ("properties/2", "Laxton - GA4"),
    )

    body = client.post(
        _URL.format(client_id=client_id, provider="reporting_ninja"), json={}
    ).json()

    assert body["status"] == "needs_choice"
    assert body["applied"] is None
    assert len(body["candidates"]) == 2
    # Кандидат несе готовий конфіг — фронт застосує його наявним PUT.
    assert body["candidates"][0]["integration_config"]["account_id"].startswith("properties/")
    assert ClientRepository(db).get_integration(client_id, Provider.reporting_ninja) is None


def test_dry_run_never_writes(client, db, monkeypatch):
    client_id = _seed_client(db)
    _patch_ninja(monkeypatch, ("sc-domain:laxton.se", "sc-domain:laxton.se"))

    body = client.post(
        _URL.format(client_id=client_id, provider="gsc"), json={"apply": False}
    ).json()

    assert body["status"] == "needs_choice"
    assert body["applied"] is None
    assert body["candidates"]
    assert ClientRepository(db).get_integration(client_id, Provider.gsc) is None


def test_already_connected_integration_is_left_untouched(client, db, monkeypatch):
    client_id = _seed_client(db)
    ClientRepository(db).upsert_integration(
        client_id,
        Provider.gsc,
        enabled=True,
        source_mode=SourceMode.api,
        config={"connection_key": "keep@example.com", "account_id": "sc-domain:keep.me"},
    )
    db.commit()

    def _explode(*_args, **_kwargs):
        raise AssertionError("must not query the provider for a connected integration")

    monkeypatch.setattr(discovery, "list_ninja_accounts", _explode)

    body = client.post(_URL.format(client_id=client_id, provider="gsc"), json={}).json()

    assert body["status"] == "already_configured"
    stored = ClientRepository(db).get_integration(client_id, Provider.gsc)
    assert stored is not None
    assert stored.config_json["account_id"] == "sc-domain:keep.me"


def test_provider_outage_is_a_state_not_a_500(client, db, monkeypatch):
    client_id = _seed_client(db)

    def _boom(*_args, **_kwargs):
        raise RuntimeError("https://api.reportingninja.com/v1?api_key=SUPERSECRET")

    monkeypatch.setattr(discovery, "list_ninja_accounts", _boom)

    response = client.post(_URL.format(client_id=client_id, provider="gsc"), json={})

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "unavailable"
    assert body["applied"] is None
    assert "SUPERSECRET" not in response.text
    assert ClientRepository(db).get_integration(client_id, Provider.gsc) is None


def test_response_carries_no_secret_material(client, db, monkeypatch):
    client_id = _seed_client(db)
    _patch_ninja(monkeypatch, ("sc-domain:laxton.se", "sc-domain:laxton.se"))
    registry.register_adapter(_OkAdapter(Provider.gsc))

    response = client.post(_URL.format(client_id=client_id, provider="gsc"), json={})

    lowered = response.text.lower()
    for marker in ("api_key", "password", "secret", "authorization", "cookie"):
        assert marker not in lowered
