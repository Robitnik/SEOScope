"""`app.urlinspector.suggest.suggest_urls` — two-stage type-ahead: client
homepage while the host is still being typed, sitemap pages once it
resolves (task-61 suggest follow-up)."""

from __future__ import annotations

from app.db.models import Client, MonitorPage
from app.urlinspector.suggest import suggest_urls


def _seed_client(session, *, domain="acme.com", name="Acme") -> Client:
    client = Client(name=name, slug=name.lower(), domain=domain)
    session.add(client)
    session.commit()
    return client


def _seed_page(session, client, url, *, in_sitemap=True):
    session.add(MonitorPage(client_id=client.id, url=url, in_sitemap=in_sitemap))
    session.commit()


class TestDomainStage:
    def test_short_query_returns_nothing(self, session):
        _seed_client(session)
        assert suggest_urls(session, q="a") == []

    def test_partial_domain_suggests_homepage(self, session):
        _seed_client(session, domain="nysapphire.com", name="SAPHNY")
        assert suggest_urls(session, q="nysapph") == ["https://nysapphire.com/"]

    def test_partial_name_also_matches(self, session):
        _seed_client(session, domain="nysapphire.com", name="SAPHNY")
        assert suggest_urls(session, q="saph") == ["https://nysapphire.com/"]

    def test_no_match_is_empty(self, session):
        _seed_client(session, domain="nysapphire.com", name="SAPHNY")
        assert suggest_urls(session, q="zzzz") == []


class TestPageStage:
    def test_resolved_host_suggests_sitemap_pages(self, session):
        client = _seed_client(session)
        _seed_page(session, client, "https://acme.com/about")
        _seed_page(session, client, "https://acme.com/contact")
        result = suggest_urls(session, q="https://acme.com/abo")
        assert result == ["https://acme.com/about"]

    def test_fragment_matches_without_needing_a_slash_boundary(self, session):
        # Real live bug: typing "/locations" against a stored path of
        # "/gentlemens-club-locations/..." matched nothing, because the
        # slash in the fragment doesn't line up with a slash in the URL.
        client = _seed_client(session)
        _seed_page(session, client, "https://acme.com/gentlemens-club-locations/")
        result = suggest_urls(session, q="https://acme.com/locations")
        assert result == ["https://acme.com/gentlemens-club-locations/"]

    def test_non_sitemap_pages_excluded(self, session):
        client = _seed_client(session)
        _seed_page(session, client, "https://acme.com/about", in_sitemap=True)
        _seed_page(session, client, "https://acme.com/gone-page", in_sitemap=False)
        result = suggest_urls(session, q="https://acme.com/")
        assert result == ["https://acme.com/about"]

    def test_bare_host_lists_pages_unfiltered_by_path(self, session):
        client = _seed_client(session)
        _seed_page(session, client, "https://acme.com/about")
        _seed_page(session, client, "https://acme.com/contact")
        result = suggest_urls(session, q="acme.com/")
        assert set(result) == {"https://acme.com/about", "https://acme.com/contact"}
