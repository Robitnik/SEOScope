"""`app.urlinspector.resolve` — URL validation and URL -> client resolution
(task-61 §0.6/§2.2), pure functions, no session."""

from __future__ import annotations

import pytest

from app.db.models import Client
from app.urlinspector.resolve import (
    UrlValidationError,
    parse_input_url,
    resolve_client_for_url,
)


def _client(domain: str, name: str = "Acme") -> Client:
    return Client(name=name, slug=name.lower(), domain=domain)


class TestParseInputUrl:
    def test_trims_and_normalizes(self):
        assert parse_input_url("  https://x.com/a  ") == "https://x.com/a"

    def test_adds_scheme_when_missing(self):
        assert parse_input_url("x.com/a").startswith("https://")

    def test_empty_raises(self):
        with pytest.raises(UrlValidationError) as exc:
            parse_input_url("")
        assert exc.value.code == "empty"

    def test_too_long_raises(self):
        with pytest.raises(UrlValidationError) as exc:
            parse_input_url("https://x.com/" + "a" * 3000)
        assert exc.value.code == "invalid_url"

    def test_unsupported_scheme_raises(self):
        with pytest.raises(UrlValidationError) as exc:
            parse_input_url("javascript:alert(1)")
        assert exc.value.code == "unsupported_scheme"

    def test_no_host_raises(self):
        with pytest.raises(UrlValidationError) as exc:
            parse_input_url("https:///a")
        assert exc.value.code == "invalid_url"


class TestResolveClientForUrl:
    def test_exact_domain_match(self):
        clients = [_client("nysapphire.com", "SAPHNY")]
        result = resolve_client_for_url("https://www.nysapphire.com/a", clients)
        assert result.client is not None
        assert result.client.name == "SAPHNY"
        assert result.match == "exact"

    def test_www_stored_domain_matches_bare_input(self):
        clients = [_client("www.nysapphire.com", "SAPHNY")]
        result = resolve_client_for_url("https://nysapphire.com/a", clients)
        assert result.match == "exact"

    def test_subdomain_match(self):
        # Real live case (task-61 §0.6): SAPHNY's own GSC data carries a
        # `blog.nysapphire.com` host that has no separate `clients` row.
        clients = [_client("nysapphire.com", "SAPHNY")]
        result = resolve_client_for_url("https://blog.nysapphire.com/post", clients)
        assert result.client is not None
        assert result.match == "subdomain"

    def test_unknown_domain(self):
        clients = [_client("nysapphire.com", "SAPHNY")]
        result = resolve_client_for_url("https://unrelated.com/a", clients)
        assert result.client is None
        assert result.match == "unknown_domain"

    def test_exact_match_preferred_over_subdomain(self):
        # A client literally named after another's subdomain must not steal
        # the match — exact domain rows are checked in a first full pass.
        clients = [_client("shop.example.com", "SHOP"), _client("example.com", "MAIN")]
        result = resolve_client_for_url("https://shop.example.com/a", clients)
        assert result.client is not None
        assert result.client.name == "SHOP"
        assert result.match == "exact"

    def test_no_clients_at_all(self):
        result = resolve_client_for_url("https://x.com/a", [])
        assert result.client is None
        assert result.match == "unknown_domain"
