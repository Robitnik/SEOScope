from __future__ import annotations

from app.db.repositories.clients import ClientRepository
from app.integrations.aiseotools_sync import sync_ai_seo_tools_integrations
from app.schemas.enums import Provider


class _FakeNinjaClient:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def list_connections(self, integration_id: str):
        if integration_id == "google_search_console":
            return [
                {
                    "connection_key": "seo@example.com",
                    "accounts": [
                        {
                            "account_id": "sc-domain:nychearing.com",
                            "account_name": "sc-domain:nychearing.com",
                        }
                    ],
                }
            ]
        return [
            {
                "connection_key": "seo@example.com",
                "accounts": [
                    {
                        "account_id": "properties/123",
                        "account_name": "NYC Hearing Associate",
                    }
                ],
            }
        ]


def test_sync_configures_provider_references_from_aiseotools(
    session_factory, monkeypatch
):
    from seo_audit.clientreport import api_client

    monkeypatch.setattr(api_client, "ReportingNinjaClient", _FakeNinjaClient)
    with session_factory() as session:
        created = ClientRepository(session).create(
            name="NYCHA",
            slug="nycha",
            domain="nychearing.com",
            market="US",
        )
        session.commit()
        client_id = created.id

    report = sync_ai_seo_tools_integrations(
        session_factory=session_factory, allowed_domains={"nychearing.com"}
    )

    assert report.configured == 4
    assert report.warnings == []
    with session_factory() as session:
        clients = ClientRepository(session)
        assert clients.get_integration(client_id, Provider.ahrefs).config_json["domain"] == (
            "nychearing.com"
        )
        semrush = clients.get_integration(client_id, Provider.semrush).config_json
        assert semrush["profile"] == "semrush"
        assert semrush["profile_store"] == "semrush"
        gsc = clients.get_integration(client_id, Provider.gsc)
        assert gsc.config_json["property"] == "sc-domain:nychearing.com"
        assert gsc.config_json["profile_store"] == "semrush"
        ga4 = clients.get_integration(client_id, Provider.reporting_ninja)
        assert ga4.config_json["account_id"] == "properties/123"
