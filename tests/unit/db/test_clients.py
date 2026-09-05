"""ClientRepository CRUD + integration upsert."""

from __future__ import annotations

from datetime import UTC, datetime

from app.db.repositories.clients import ClientRepository
from app.schemas.enums import ClientStatus, Provider, SourceMode


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


def test_create_defaults_to_not_dev(session):
    repo = ClientRepository(session)

    client = repo.create(name="Acme", slug="acme", domain="acme.com")

    assert client.is_dev is False


def test_create_dev_client(session):
    repo = ClientRepository(session)

    client = repo.create(
        name="Alpha Version", slug="alpha-dev", domain="alfa.tyvdev.com", is_dev=True
    )

    assert client.is_dev is True
    assert repo.get(client.id).is_dev is True


# --- client lifecycle status (task-34) --------------------------------------


def test_create_defaults_to_active_without_a_status_date(session):
    repo = ClientRepository(session)

    client = repo.create(name="Acme", slug="acme", domain="acme.com")

    assert client.status == ClientStatus.active
    # Створення — не зміна статусу, тож дати бути не повинно.
    assert client.status_changed_at is None
    assert client.status_note is None


def test_set_status_records_the_change_date_and_note(session):
    repo = ClientRepository(session)
    client = repo.create(name="Acme", slug="acme", domain="acme.com")
    now = datetime(2026, 7, 12, 9, 30, tzinfo=UTC)

    paused = repo.set_status(client.id, ClientStatus.paused, note=" payment stopped ", now=now)

    assert paused.status == ClientStatus.paused
    assert paused.status_changed_at == now
    assert paused.status_note == "payment stopped"


def test_set_status_keeps_the_original_date_when_the_status_is_unchanged(session):
    repo = ClientRepository(session)
    client = repo.create(name="Acme", slug="acme", domain="acme.com")
    paused_at = datetime(2026, 7, 12, 9, 30, tzinfo=UTC)
    repo.set_status(client.id, ClientStatus.paused, note="payment stopped", now=paused_at)

    # Оператор лише виправив нотатку — «paused since» не має зсуватись.
    updated = repo.set_status(
        client.id,
        ClientStatus.paused,
        note="payment stopped 01.07",
        now=datetime(2026, 8, 1, 12, 0, tzinfo=UTC),
    )

    assert updated.status_changed_at == paused_at
    assert updated.status_note == "payment stopped 01.07"


def test_set_status_clears_the_note_when_it_is_blank(session):
    repo = ClientRepository(session)
    client = repo.create(name="Acme", slug="acme", domain="acme.com")
    repo.set_status(client.id, ClientStatus.paused, note="payment stopped")

    resumed = repo.set_status(client.id, ClientStatus.active, note="   ")

    assert resumed.status == ClientStatus.active
    assert resumed.status_note is None


def test_list_collectable_excludes_paused_but_list_active_keeps_them(session):
    repo = ClientRepository(session)
    active = repo.create(name="Acme", slug="acme", domain="acme.com")
    paused = repo.create(name="Old Co", slug="old-co", domain="old.com")
    disabled = repo.create(name="Gone", slug="gone", domain="gone.com", enabled=False)
    repo.set_status(paused.id, ClientStatus.paused)

    collectable = [c.id for c in repo.list_collectable()]
    visible = [c.id for c in repo.list_active()]

    assert collectable == [active.id]
    # Паузований лишається видимим — у цьому весь сенс статусу.
    assert sorted(visible) == sorted([active.id, paused.id])
    assert disabled.id not in visible


def test_set_status_on_unknown_client_raises(session):
    repo = ClientRepository(session)

    try:
        repo.set_status("does-not-exist", ClientStatus.paused)
    except LookupError as exc:
        assert "does-not-exist" in str(exc)
    else:  # pragma: no cover - the call above must raise
        raise AssertionError("set_status must raise for an unknown client")


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
