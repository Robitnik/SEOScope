"""Заголовки відповіді читаються без урахування регістру.

Жива регресія 2026-08-12: `alphastyle.store` (Shopify за Cloudflare) віддає
`content-encoding: gzip` малими літерами — як і вимагає HTTP/2. Код будував
звичайний `dict` із `response.headers.items()` і питав `"Content-Encoding"`,
тобто отримував `None` і клав у сховище **стиснені байти**. Наслідок на живих
даних: усі 355 сторінок цього клієнта мали `title = NULL`, порожні meta robots
і canonical — тобто `noindex` на цьому сайті монітор би не побачив.

Той самий dict читає `X-Robots-Tag` (де живе `noindex`) і `Content-Type`.
"""

from __future__ import annotations

import gzip

from app.monitor.fetcher import Headers


def test_lookup_is_case_insensitive_in_both_directions():
    headers = Headers({"content-encoding": "gzip", "X-Robots-Tag": "noindex"})
    assert headers.get("Content-Encoding") == "gzip"
    assert headers.get("content-encoding") == "gzip"
    assert headers.get("x-robots-tag") == "noindex"
    assert headers.get("X-ROBOTS-TAG") == "noindex"


def test_missing_header_still_returns_the_default():
    headers = Headers({"content-type": "text/html"})
    assert headers.get("Content-Encoding", "") == ""
    assert headers.get("Nope") is None


def test_contains_is_case_insensitive_too():
    headers = Headers({"content-encoding": "gzip"})
    assert "Content-Encoding" in headers
    assert "content-encoding" in headers
    assert "Content-Length" not in headers


def test_original_casing_is_preserved_for_display():
    # Ключі лишаються такими, як їх прислав сервер: заголовки показуються
    # оператору, і вигадувати за нього іншу форму запису ми не маємо права.
    headers = Headers({"content-encoding": "gzip"})
    assert list(headers) == ["content-encoding"]


def test_lowercase_content_encoding_reaches_the_decompression_branch(monkeypatch):
    """Наскрізна перевірка гілки, яка мовчки не спрацьовувала на живому."""
    from app.monitor import fetcher as fetcher_mod

    payload = b"User-agent: *\nDisallow: /private/\n"

    class _FakeResponse:
        status = 200
        headers = {"content-encoding": "gzip", "content-type": "text/plain"}

        def read(self, _size):
            return gzip.compress(payload)

        def geturl(self):
            return "https://shop.example/robots.txt"

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    class _FakeOpener:
        def open(self, _request, timeout=None):  # noqa: ARG002
            return _FakeResponse()

    # The SSRF guard resolves the host for real; `shop.example` does not exist.
    # Neutralised here so the test exercises the decompression branch, not DNS.
    monkeypatch.setattr(fetcher_mod, "assert_public_http_url", lambda *a, **k: None)
    monkeypatch.setattr(fetcher_mod.urllib.request, "build_opener", lambda *a, **k: _FakeOpener())

    result = fetcher_mod.fetch(
        "https://shop.example/robots.txt",
        throttle=fetcher_mod.HostThrottle(),
        stats=fetcher_mod.FetchStats(),
    )
    assert result.body == payload, "gzip body must be decompressed despite the lowercase header"
