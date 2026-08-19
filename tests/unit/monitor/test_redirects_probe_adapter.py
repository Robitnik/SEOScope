"""Blackbox-проба редіректів (task-51 §3.2.3) — для сайтів без

`redirection/v1` (Shopify, WP без плагіна). Мережа інжектується фейковим
fetcher-ом у формі `app.tools.redirects.trace.RawResponse`, той самий тип,
що вже покритий тестами трейсера (task-29).
"""

from __future__ import annotations

import urllib.error

from app.monitor.redirects.probe import probe_redirects
from app.tools.http_safety import UnsafeUrlError
from app.tools.redirects.trace import RawResponse


class _FakeFetcher:
    def __init__(self, routes: dict[str, RawResponse]) -> None:
        self.routes = routes
        self.requested: list[str] = []

    def __call__(self, url: str) -> RawResponse:
        self.requested.append(url)
        if url in self.routes:
            return self.routes[url]
        raise urllib.error.URLError("no route configured")


def _redirect(location: str, *, status: int = 301, redirect_by: str | None = None) -> RawResponse:
    headers = [("location", location)]
    if redirect_by is not None:
        headers.append(("x-redirect-by", redirect_by))
    return RawResponse(status=status, headers=tuple(headers))


def _page(status: int = 200) -> RawResponse:
    return RawResponse(status=status, headers=(("content-type", "text/html"),))


def test_a_301_becomes_a_rule() -> None:
    fetcher = _FakeFetcher(
        {"https://client.example/old/": _redirect("https://client.example/new/")}
    )
    result = probe_redirects(["https://client.example/old/"], fetcher=fetcher)
    assert result.ok is True
    assert result.source == "probe"
    assert len(result.rules) == 1
    rule = result.rules[0]
    assert rule.source_url == "https://client.example/old/"
    assert rule.target_url == "https://client.example/new/"
    assert rule.http_code == 301
    assert rule.rule_key == rule.source_url
    assert rule.external_id is None


def test_a_direct_200_produces_no_rule() -> None:
    fetcher = _FakeFetcher({"https://client.example/ok/": _page()})
    result = probe_redirects(["https://client.example/ok/"], fetcher=fetcher)
    assert result.ok is True
    assert result.rules == ()


def test_x_redirect_by_header_is_captured_as_origin_hint() -> None:
    response = _redirect("https://client.example/new/", redirect_by="redirection")
    fetcher = _FakeFetcher({"https://client.example/old/": response})
    result = probe_redirects(["https://client.example/old/"], fetcher=fetcher)
    assert result.rules[0].title == "via: redirection"


def test_missing_x_redirect_by_header_leaves_title_none() -> None:
    fetcher = _FakeFetcher(
        {"https://client.example/old/": _redirect("https://client.example/new/")}
    )
    result = probe_redirects(["https://client.example/old/"], fetcher=fetcher)
    assert result.rules[0].title is None


def test_only_the_first_hop_is_used_second_hop_is_not_followed() -> None:
    # probe.py читає лише першу відповідь одного запиту — воно не йде за
    # Location саме, бо для монітора цікавий факт "цей URL зараз редіректить
    # і куди", не повний ланцюжок (те, для чого існує окремий Redirect Checker).
    fetcher = _FakeFetcher({"https://client.example/a/": _redirect("https://client.example/b/")})
    result = probe_redirects(["https://client.example/a/"], fetcher=fetcher)
    assert fetcher.requested == ["https://client.example/a/"]
    assert result.rules[0].target_url == "https://client.example/b/"


def test_one_failed_url_does_not_discard_the_others() -> None:
    fetcher = _FakeFetcher(
        {"https://client.example/good/": _redirect("https://client.example/new/")}
    )
    result = probe_redirects(
        ["https://client.example/good/", "https://client.example/bad/"], fetcher=fetcher
    )
    assert result.ok is True
    assert len(result.rules) == 1
    assert result.detail is not None and "1 failed" in result.detail


def test_all_urls_failing_is_unavailable_not_empty_rules() -> None:
    fetcher = _FakeFetcher({})
    result = probe_redirects(
        ["https://client.example/a/", "https://client.example/b/"], fetcher=fetcher
    )
    assert result.ok is False
    assert result.unavailable_reason == "unavailable"


def test_empty_url_list_is_unavailable() -> None:
    result = probe_redirects([], fetcher=_FakeFetcher({}))
    assert result.ok is False
    assert result.unavailable_reason == "unavailable"


def test_unsafe_url_error_on_one_url_counts_as_a_failure_not_a_crash() -> None:
    class _UnsafeFetcher:
        def __call__(self, url: str) -> RawResponse:
            raise UnsafeUrlError("blocked")

    result = probe_redirects(["https://client.example/x/"], fetcher=_UnsafeFetcher())
    assert result.ok is False
    assert result.unavailable_reason == "unavailable"


def test_302_and_308_are_also_recognized_as_redirects() -> None:
    fetcher = _FakeFetcher(
        {
            "https://client.example/a/": _redirect("https://client.example/a2/", status=302),
            "https://client.example/b/": _redirect("https://client.example/b2/", status=308),
        }
    )
    result = probe_redirects(
        ["https://client.example/a/", "https://client.example/b/"], fetcher=fetcher
    )
    assert {r.http_code for r in result.rules} == {302, 308}


def test_4xx_and_5xx_produce_no_rule_but_are_not_failures() -> None:
    fetcher = _FakeFetcher({"https://client.example/gone/": _page(status=404)})
    result = probe_redirects(["https://client.example/gone/"], fetcher=fetcher)
    assert result.ok is True
    assert result.rules == ()
