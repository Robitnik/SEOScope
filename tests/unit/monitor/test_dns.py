"""DoH client (task-35 §5) — mocked responses, no real network in unit tests
(the live resolvers are exercised manually/in `tests/live`)."""

from __future__ import annotations

import json
import urllib.error

from app.monitor import dns as dns_mod
from app.monitor.dns import extract_google_site_verification_tokens, resolve


class _FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._body = json.dumps(payload).encode()

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_resolve_parses_a_records(monkeypatch):
    def fake_urlopen(request, timeout):
        return _FakeResponse({"Status": 0, "Answer": [{"type": 1, "TTL": 300, "data": "1.2.3.4"}]})

    monkeypatch.setattr(dns_mod.urllib.request, "urlopen", fake_urlopen)
    result = resolve("example.com", "A")
    assert result.available
    assert result.values == ("1.2.3.4",)
    assert result.ttl == 300
    assert result.resolver == "dns.google"


def test_resolve_falls_back_to_second_resolver_when_first_fails(monkeypatch):
    calls = []

    def fake_urlopen(request, timeout):
        calls.append(request.full_url)
        if "dns.google" in request.full_url:
            raise urllib.error.URLError("boom")
        return _FakeResponse({"Status": 0, "Answer": [{"type": 1, "TTL": 60, "data": "9.9.9.9"}]})

    monkeypatch.setattr(dns_mod.urllib.request, "urlopen", fake_urlopen)
    result = resolve("example.com", "A")
    assert result.available
    assert result.resolver == "cloudflare-dns.com"
    assert len(calls) == 2


def test_resolve_unavailable_when_every_resolver_fails(monkeypatch):
    def fake_urlopen(request, timeout):
        raise urllib.error.URLError("boom")

    monkeypatch.setattr(dns_mod.urllib.request, "urlopen", fake_urlopen)
    result = resolve("example.com", "A")
    assert result.available is False
    assert result.values == ()
    assert result.resolver is None


def test_nxdomain_is_a_valid_empty_answer_not_unavailable(monkeypatch):
    def fake_urlopen(request, timeout):
        return _FakeResponse({"Status": 3, "Answer": []})

    monkeypatch.setattr(dns_mod.urllib.request, "urlopen", fake_urlopen)
    result = resolve("nope.example.com", "A")
    assert result.available is True
    assert result.values == ()


def test_txt_values_have_quotes_stripped(monkeypatch):
    def fake_urlopen(request, timeout):
        return _FakeResponse(
            {"Status": 0, "Answer": [{"type": 16, "TTL": 300, "data": '"v=spf1 ~all"'}]}
        )

    monkeypatch.setattr(dns_mod.urllib.request, "urlopen", fake_urlopen)
    result = resolve("example.com", "TXT")
    assert result.values == ("v=spf1 ~all",)


def test_multiple_google_site_verification_txt_records_all_extracted():
    # Live regression: alphastyle.store carries two verification TXT records.
    tokens = extract_google_site_verification_tokens(
        ("google-site-verification=tokenA", "google-site-verification=tokenB", "v=spf1 ~all")
    )
    assert tokens == ["tokenA", "tokenB"]


def test_disappearing_verification_token_is_detectable_by_set_diff():
    old = frozenset(extract_google_site_verification_tokens(("google-site-verification=abc",)))
    new = frozenset(extract_google_site_verification_tokens(("v=spf1 ~all",)))
    assert old - new == {"abc"}
