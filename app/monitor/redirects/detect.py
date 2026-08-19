"""Автовизначення джерела редіректів із `/wp-json/` (task-51 §3.2.4).

Дешевий анонімний запит: WordPress завжди віддає `namespaces` у корені REST
API, і плагіни реєструють себе там під власним namespace — той самий трюк,
яким живою перевіркою 2026-08-19 намацано 10/14 клієнтів портфелю з
`redirection/v1`, а Shopify-клієнт (alphastyle.store) відсіявся через 404 на
`/wp-json/` узагалі.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Callable
from enum import StrEnum

from app.tools.http_safety import (
    NO_CACHE_HEADERS,
    USER_AGENT,
    UnsafeUrlError,
    assert_public_http_url,
)

__all__ = ["RedirectSource", "detect_source", "fetch_namespaces", "source_from_namespaces"]

REQUEST_TIMEOUT_SECONDS = 10.0
MAX_BODY_BYTES = 2 * 1024 * 1024
KNOWN_NAMESPACE = "redirection/v1"


class RedirectSource(StrEnum):
    redirection = "redirection"
    probe = "probe"


NamespaceFetcher = Callable[[str], bytes | None]


def fetch_namespaces(base_url: str) -> bytes | None:
    """Сире тіло `/wp-json/`. `None` — не змогли отримати (мережа, SSRF-гард,

    статус не 2xx) — саме тому, а не через виняток: виклику це не привід
    падати, лише підстава відповісти `probe`.
    """
    url = f"{base_url}/wp-json/"
    try:
        assert_public_http_url(url)
    except UnsafeUrlError:
        return None
    request = urllib.request.Request(  # noqa: S310 - схема перевірена гардом вище
        url, headers={"User-Agent": USER_AGENT, "Accept": "application/json", **NO_CACHE_HEADERS}
    )
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:  # noqa: S310
            if response.status != 200:
                return None
            body: bytes = response.read(MAX_BODY_BYTES + 1)[:MAX_BODY_BYTES]
            return body
    except (urllib.error.URLError, TimeoutError, OSError):
        return None


def source_from_namespaces(body: bytes) -> RedirectSource:
    try:
        payload = json.loads(body.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        return RedirectSource.probe
    if not isinstance(payload, dict):
        return RedirectSource.probe
    namespaces = payload.get("namespaces")
    if isinstance(namespaces, list) and KNOWN_NAMESPACE in namespaces:
        return RedirectSource.redirection
    return RedirectSource.probe


def detect_source(base_url: str, *, fetcher: NamespaceFetcher | None = None) -> RedirectSource:
    """`probe` для все, що не впевнено `redirection/v1`: і коли `/wp-json/`

    відповів без цього namespace (інший плагін чи чистий WP), і коли сайт
    узагалі не на WordPress (Shopify тощо, `/wp-json/` дає 404). В обох
    випадках єдине, що лишається, — проба відомих URL (§3.2.3).
    """
    fetch = fetcher or fetch_namespaces
    body = fetch(base_url)
    if body is None:
        return RedirectSource.probe
    return source_from_namespaces(body)
