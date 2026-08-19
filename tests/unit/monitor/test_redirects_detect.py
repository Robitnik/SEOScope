"""Автовизначення джерела редіректів (task-51 §3.2.4) — з реальних форм

`/wp-json/` живого портфелю (виміряно 2026-08-19): `effyscafe.com` та інші
9 клієнтів мають `redirection/v1` у `namespaces`; `goreport.com` та ще двоє
мають лише `wp/v2`+`yoast/v1`; `alphastyle.store` (Shopify) взагалі не
віддає `/wp-json/`.
"""

from __future__ import annotations

import json

from app.monitor.redirects.detect import RedirectSource, detect_source, source_from_namespaces


def _namespaces_body(namespaces: list[str]) -> bytes:
    return json.dumps({"name": "Site", "namespaces": namespaces}).encode()


def test_known_namespace_present_detects_redirection() -> None:
    body = _namespaces_body(["oembed/1.0", "redirection/v1", "wp/v2", "yoast/v1"])
    assert source_from_namespaces(body) is RedirectSource.redirection


def test_absent_known_namespace_falls_back_to_probe() -> None:
    body = _namespaces_body(["oembed/1.0", "wp/v2", "yoast/v1"])
    assert source_from_namespaces(body) is RedirectSource.probe


def test_malformed_json_falls_back_to_probe_without_raising() -> None:
    assert source_from_namespaces(b"not json at all") is RedirectSource.probe


def test_json_that_is_not_an_object_falls_back_to_probe() -> None:
    assert source_from_namespaces(b"[1, 2, 3]") is RedirectSource.probe


def test_namespaces_field_missing_or_wrong_type_falls_back_to_probe() -> None:
    no_field = json.dumps({"name": "Site"}).encode()
    wrong_type = json.dumps({"namespaces": "not-a-list"}).encode()
    assert source_from_namespaces(no_field) is RedirectSource.probe
    assert source_from_namespaces(wrong_type) is RedirectSource.probe


def test_detect_source_uses_the_injected_fetcher() -> None:
    calls: list[str] = []

    def fetcher(base_url: str) -> bytes | None:
        calls.append(base_url)
        return _namespaces_body(["redirection/v1"])

    result = detect_source("https://client.example", fetcher=fetcher)
    assert result is RedirectSource.redirection
    assert calls == ["https://client.example"]


def test_detect_source_treats_unreachable_site_as_probe_not_a_crash() -> None:
    # Shopify (alphastyle.store) кейс: /wp-json/ узагалі не відповідає 200 —
    # fetch_namespaces повертає None, detect_source не кидає виняток.
    result = detect_source("https://shopify-client.example", fetcher=lambda base_url: None)
    assert result is RedirectSource.probe
