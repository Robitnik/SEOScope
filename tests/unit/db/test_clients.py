"""ClientRepository CRUD + integration upsert."""

from __future__ import annotations

from app.db.repositories.clients import ClientRepository
from app.schemas.enums import Provider, SourceMode


def test_create_and_get(session):
    repo = ClientRepository(session)
    client = repo.create(name="Acme", slug="acme", domain="acme.com")

    assert repo.get(client.id).slug == "acme"


def test_list_active_excludes_disabled(session):
    repo = ClientRepository(session)
    repo.create(name="Acme", slug="acme", domain="acme.com")
    disabled = repo.create(name="Old Co", slug="old-co", domain="old.com", enabled=False)

    active = repo.list_active()

    assert disabled.id not in [c.id for c in active]


def test_set_logo(session):
    repo = ClientRepository(session)
    client = repo.create(name="Acme", slug="acme", domain="acme.com")

    updated = repo.set_logo(client.id, "logos/acme.png")

    assert updated.logo_path == "logos/acme.png"


def test_upsert_integration_creates_then_updates(session):
    repo = ClientRepository(session)
    client = repo.create(name="Acme", slug="acme", domain="acme.com")

    created = repo.upsert_integration(
        client.id, Provider.ahrefs, source_mode=SourceMode.api, config={"project_id": "1"}
    )
    assert created.config_json == {"project_id": "1"}

    updated = repo.upsert_integration(
        client.id, Provider.ahrefs, source_mode=SourceMode.browser, config={"project_id": "2"}
    )
    assert updated.id == created.id
    assert updated.source_mode == SourceMode.browser
    assert updated.config_json == {"project_id": "2"}

    assert repo.get_integration(client.id, Provider.ahrefs).id == created.id
    assert repo.get_integration(client.id, Provider.semrush) is None
