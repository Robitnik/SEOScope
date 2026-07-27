"""Перевірка «чи новий alt уже на сторінці» — запит без кешу.

Чому cache-buster обовʼязковий: у клієнтів стоїть кілька шарів кешу (плагін
сторінок, Varnish, CDN), і звичайний `GET` віддав би HTML, збережений до
правки alt. Рандомний query-параметр майже завжди пробиває page-cache, а
`Cache-Control: no-cache`/`Pragma` просить не віддавати кеш проксі. Це
**зменшує**, але не прибирає ризик edge-кешу, тому UI прямо про це пише, а
результат завжди має час перевірки.

Мережа тут — stdlib `urllib.request`, HTML — stdlib `html.parser`. Свідомо
без `httpx`/`lxml`: вони є в образі лише транзитивно через optional-групу
`local` (AISeoTools), тож на CI/деплої без bind-mount інструмент з ними
зламався б.

SSRF-гард, browser-UA, cache-buster і no-cache-заголовки живуть у спільному
`app.tools.http_safety` (витягнуті task-29 для Redirect Checker, без зміни
поведінки) і реекспортуються звідси — імена, на які спираються виклики й
тести цього інструмента, лишились ті самі.
"""

from __future__ import annotations

import re
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from html.parser import HTMLParser
from urllib.parse import urljoin

from app.tools.alt_text.wp_media import derive_search_key
from app.tools.http_safety import (
    CACHE_BUSTER_PARAM,
    MAX_REDIRECTS,
    NO_CACHE_HEADERS,
    USER_AGENT,
    GuardedRedirectHandler,
    UnsafeUrlError,
    assert_public_http_url,
    build_cache_busted_url,
)

__all__ = [
    "CACHE_BUSTER_PARAM",
    "MAX_BODY_BYTES",
    "MAX_REDIRECTS",
    "REQUEST_TIMEOUT_SECONDS",
    "USER_AGENT",
    "FetchedPage",
    "Fetcher",
    "PageImage",
    "UnsafeUrlError",
    "VerifyResult",
    "VerifyStatus",
    "assert_public_http_url",
    "build_cache_busted_url",
    "fetch_page",
    "find_page_images",
    "normalize_alt",
    "verify_alt",
]

REQUEST_TIMEOUT_SECONDS = 15.0
MAX_BODY_BYTES = 5 * 1024 * 1024

_WHITESPACE = re.compile(r"\s+")
# Атрибути, під якими lazy-load-плагіни ховають справжній src.
_SRC_ATTRIBUTES = ("src", "data-src", "data-lazy-src", "data-original", "data-lazy", "data-echo")
_SRCSET_ATTRIBUTES = ("srcset", "data-srcset", "data-lazy-srcset")


class VerifyStatus(StrEnum):
    """Окремі стани, а не «ок / не ок» (PLAN §12)."""

    match = "match"
    mismatch = "mismatch"
    empty_alt = "empty_alt"
    image_not_found_on_page = "image_not_found_on_page"
    fetch_failed = "fetch_failed"


@dataclass(frozen=True)
class FetchedPage:
    http_status: int
    html: str
    final_url: str
    truncated: bool = False


@dataclass(frozen=True)
class VerifyResult:
    status: VerifyStatus
    found_alt: str | None
    http_status: int | None
    detail: str | None
    checked_url: str
    images_on_page: int = 0
    candidates_matched: int = 0


Fetcher = Callable[[str], FetchedPage]


def fetch_page(url: str) -> FetchedPage:
    """Завантажити HTML без кешу. Кидає `urllib.error.*` / `UnsafeUrlError`."""
    assert_public_http_url(url)
    request = urllib.request.Request(  # noqa: S310 - схема перевірена гардом вище
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en-US,en;q=0.9",
            **NO_CACHE_HEADERS,
        },
    )
    opener = urllib.request.build_opener(GuardedRedirectHandler)
    with opener.open(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
        raw = response.read(MAX_BODY_BYTES + 1)
        truncated = len(raw) > MAX_BODY_BYTES
        charset = response.headers.get_content_charset() or "utf-8"
        return FetchedPage(
            http_status=response.status,
            html=raw[:MAX_BODY_BYTES].decode(charset, errors="replace"),
            final_url=response.geturl(),
            truncated=truncated,
        )


# --- HTML: знайти картинку й її alt -------------------------------------------


@dataclass
class PageImage:
    sources: list[str]
    alt: str | None


class _ImageCollector(HTMLParser):
    """Збирає `<img>` (і `<source>` у `<picture>`) з усіма варіантами src."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.images: list[PageImage] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag not in {"img", "source"}:
            return
        values = {name.lower(): (value or "") for name, value in attrs}
        sources: list[str] = []
        for name in _SRC_ATTRIBUTES:
            candidate = values.get(name, "").strip()
            if candidate:
                sources.append(candidate)
        for name in _SRCSET_ATTRIBUTES:
            for entry in values.get(name, "").split(","):
                candidate = entry.strip().split(" ", 1)[0]
                if candidate:
                    sources.append(candidate)
        if not sources:
            return
        # `<source>` не несе alt — його дає сусідній `<img>`; тому alt лишається
        # None, і такий елемент може лише підтвердити «картинка на сторінці є».
        alt = values.get("alt") if tag == "img" else None
        self.images.append(PageImage(sources=sources, alt=alt))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)


def normalize_alt(value: str) -> str:
    """Порівнюємо alt без різниці у пробілах/переносах, але з регістром."""
    return _WHITESPACE.sub(" ", value).strip()


def find_page_images(html: str) -> list[PageImage]:
    collector = _ImageCollector()
    collector.feed(html)
    collector.close()
    return collector.images


def _stem(url: str) -> str:
    return derive_search_key(url).search_key.casefold()


# --- публічна перевірка -------------------------------------------------------


def verify_alt(
    page_url: str,
    image_url: str,
    expected_alt: str,
    *,
    fetcher: Fetcher | None = None,
    nonce: str | None = None,
) -> VerifyResult:
    """Перевірити, чи на `page_url` у картинки `image_url` стоїть `expected_alt`.

    Картинку шукаємо **не** рівністю URL, а рівністю дерівованого stem: на
    сторінці майже завжди стоїть resize-варіант (`-1024x683`), тоді як у звіті
    лежить інший розмір або оригінал.
    """
    if not expected_alt.strip():
        raise ValueError("expected_alt is empty; nothing to compare against")

    checked_url = build_cache_busted_url(page_url, nonce)
    fetch = fetcher or fetch_page
    try:
        page = fetch(checked_url)
    except UnsafeUrlError as exc:
        return _failed(checked_url, None, str(exc))
    except urllib.error.HTTPError as exc:
        return _failed(checked_url, exc.code, f"The page returned HTTP {exc.code}.")
    except urllib.error.URLError as exc:
        return _failed(checked_url, None, f"The page could not be loaded: {exc.reason}.")
    except TimeoutError:
        return _failed(checked_url, None, "The page did not respond in time.")
    except OSError as exc:
        return _failed(checked_url, None, f"The page could not be loaded: {exc}.")

    expected_stem = _stem(image_url)
    images = find_page_images(page.html)
    if not expected_stem:
        return VerifyResult(
            status=VerifyStatus.fetch_failed,
            found_alt=None,
            http_status=page.http_status,
            detail="The image URL has no usable filename to look for.",
            checked_url=checked_url,
            images_on_page=len(images),
        )

    matched = [image for image in images if _matches(image, expected_stem, page.final_url)]
    if not matched:
        detail = "The image is not in the page HTML — it may be rendered by JavaScript."
        if page.truncated:
            detail = (
                "The image was not found in the first 5 MB of the page HTML "
                "(the response was larger and got truncated)."
            )
        return VerifyResult(
            status=VerifyStatus.image_not_found_on_page,
            found_alt=None,
            http_status=page.http_status,
            detail=detail,
            checked_url=checked_url,
            images_on_page=len(images),
        )

    expected = normalize_alt(expected_alt)
    with_alt = [image for image in matched if image.alt is not None]
    # Одна картинка може стояти на сторінці кілька разів; якщо хоч одне
    # входження вже має правильний alt — вважаємо збіг і показуємо саме його.
    exact = next((image for image in with_alt if normalize_alt(image.alt or "") == expected), None)
    chosen = exact or (with_alt[0] if with_alt else None)
    if chosen is None:
        return VerifyResult(
            status=VerifyStatus.empty_alt,
            found_alt=None,
            http_status=page.http_status,
            detail="Found the image only in a <source> element, which carries no alt.",
            checked_url=checked_url,
            images_on_page=len(images),
            candidates_matched=len(matched),
        )

    found = normalize_alt(chosen.alt or "")
    if not found:
        status = VerifyStatus.empty_alt
    elif found == expected:
        status = VerifyStatus.match
    else:
        status = VerifyStatus.mismatch
    return VerifyResult(
        status=status,
        found_alt=found,
        http_status=page.http_status,
        detail=None,
        checked_url=checked_url,
        images_on_page=len(images),
        candidates_matched=len(matched),
    )


def _matches(image: PageImage, expected_stem: str, base_url: str) -> bool:
    for source in image.sources:
        if source.startswith("data:"):
            continue
        absolute = urljoin(base_url, source)
        if _stem(absolute) == expected_stem:
            return True
    return False


def _failed(checked_url: str, http_status: int | None, detail: str) -> VerifyResult:
    return VerifyResult(
        status=VerifyStatus.fetch_failed,
        found_alt=None,
        http_status=http_status,
        detail=detail,
        checked_url=checked_url,
    )
