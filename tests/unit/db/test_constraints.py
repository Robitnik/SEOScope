"""Unique and foreign-key constraints actually fire (PLAN §6)."""

from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError

from app.db.models import Client, ClientIntegration
from app.schemas.enums import Provider


def _make_client(**overrides) -> Client:
    defaults = dict(name="Acme", slug="acme", domain="acme.com")
    defaults.update(overrides)
    return Client(**defaults)


def test_duplicate_slug_rejected(session):
    session.add(_make_client())
    session.commit()

    session.add(_make_client(domain="acme2.com"))
    with pytest.raises(IntegrityError):
        session.commit()


def test_duplicate_domain_rejected(session):
    session.add(_make_client())
    session.commit()

    session.add(_make_client(slug="acme-2"))
    with pytest.raises(IntegrityError):
        session.commit()


def test_foreign_key_enforced_on_insert(session):
    session.add(ClientIntegration(client_id="does-not-exist", provider=Provider.ahrefs))
    with pytest.raises(IntegrityError):
        session.commit()


def test_unique_client_provider_pair(session):
    client = _make_client()
    session.add(client)
    session.commit()

    session.add(ClientIntegration(client_id=client.id, provider=Provider.ahrefs))
    session.commit()

    session.add(ClientIntegration(client_id=client.id, provider=Provider.ahrefs))
    with pytest.raises(IntegrityError):
        session.commit()


def test_cascade_delete_removes_integrations(session):
    client = _make_client()
    session.add(client)
    session.commit()
    session.add(ClientIntegration(client_id=client.id, provider=Provider.semrush))
    session.commit()

    session.delete(client)
    session.commit()

    remaining = session.query(ClientIntegration).filter_by(client_id=client.id).all()
    assert remaining == []
