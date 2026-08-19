"""`resolve_base_url` + `MonitorSettings.canonical_www` (task-38 follow-up).

An operator-recorded canonical host picks the right host directly instead of
relying on the site's own redirect to correct it on every single request —
`canonical_www is None` (not yet recorded) must behave exactly as before this
field existed: `client.domain` verbatim.
"""

from __future__ import annotations

from app.db.models import Client
from app.monitor.runner import resolve_base_url


def _client(domain: str) -> Client:
    return Client(name="Acme", slug="acme", domain=domain)


def test_unknown_canonical_www_uses_domain_verbatim():
    assert resolve_base_url(_client("acme.com"), None, None) == "https://acme.com"
    assert resolve_base_url(_client("www.acme.com"), None, None) == "https://www.acme.com"


def test_canonical_www_true_adds_the_prefix_when_missing():
    assert resolve_base_url(_client("acme.com"), None, True) == "https://www.acme.com"


def test_canonical_www_true_is_a_no_op_when_already_present():
    assert resolve_base_url(_client("www.acme.com"), None, True) == "https://www.acme.com"


def test_canonical_www_false_strips_the_prefix_when_present():
    assert resolve_base_url(_client("www.acme.com"), None, False) == "https://acme.com"


def test_canonical_www_false_is_a_no_op_when_already_bare():
    assert resolve_base_url(_client("acme.com"), None, False) == "https://acme.com"


def test_override_wins_outright_regardless_of_canonical_www():
    assert (
        resolve_base_url(_client("acme.com"), "https://staging.acme.com/", True)
        == "https://staging.acme.com"
    )
