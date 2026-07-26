"""Деривація фрагмента, за яким медіатека WordPress реально знаходить файл.

**Чому повний URL не шукається.** Пошук у Media Library — це `LIKE '%term%'`
по полях вкладення (`post_title`/`post_content`/`post_excerpt`) і, у сучасних
версіях, по значенню `_wp_attached_file`. А там лежить `2024/03/hero.jpg` —
шлях і назва **оригіналу**, ніколи не повний URL і ніколи не назва з суфіксом
розміру. Тому шукати треба stem оригінального файлу, знявши всі похідні
суфікси: розміри (`-300x200`), big-image (`-scaled`), WP-редактор
(`-e1712345678901`), retina (`@2x`), плюс обгортки CDN/WebP-конвертерів.

Модуль **чистий**: ніякої мережі, ніякої БД, детермінований результат. Кожне
відрізання повертається в `stripped`, щоб UI показав оператору, *що саме*
прибрали, а не просто видав «магічний» рядок.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import parse_qs, unquote, urlsplit

# Довжина колонки `alt_text_items.search_key` (див. міграцію) — обрізаємо тут,
# а не в БД, щоб оператор побачив попередження замість тихого падіння вставки.
MAX_SEARCH_KEY_LENGTH = 512
_MIN_USEFUL_KEY_LENGTH = 4
_MIN_FALLBACK_LENGTH = 3

IMAGE_EXTENSIONS = frozenset(
    {"jpg", "jpeg", "png", "gif", "webp", "avif", "svg", "bmp", "tif", "tiff", "ico"}
)

# Query-параметри resize-проксі: несуть розмір, але не назву файлу — це сигнал,
# а не дані, тож у пошуковий фрагмент вони не входять ніколи.
_RESIZE_QUERY_KEYS = frozenset(
    {"resize", "fit", "w", "h", "width", "height", "quality", "q", "crop", "zoom", "strip"}
)

_UPLOADS_MARKER = "/wp-content/uploads/"
_PHOTON_HOST = re.compile(r"^i[0-3]\.wp\.com$", re.IGNORECASE)
_CLOUDFLARE_PREFIX = re.compile(r"^/cdn-cgi/image/[^/]+/")
_STATICALLY_PREFIX = re.compile(r"^/img/", re.IGNORECASE)
_STATICALLY_HOST = re.compile(r"^cdn\.statically\.io$", re.IGNORECASE)
# Проксі, що беруть цільову картинку query-параметром (`images.weserv.nl/?url=…`).
_URL_PARAM_KEYS = ("url", "src", "image", "u")

# Хвостові модифікатори, які WordPress (і resize-плагіни) додають до назви
# оригіналу. Порядок значення не має — цикл крутиться, доки хоч один матчиться,
# бо реальні файли комбінують їх (`hero-e1712345678901-300x200.jpg`).
_SUFFIX_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"-\d{1,5}x\d{1,5}$"),  # -300x200 (розміри WP і плагінів)
    re.compile(r"-scaled$"),  # big-image threshold, WP 5.3+
    re.compile(r"-rotated$"),
    re.compile(r"-e\d{10,13}$"),  # копія з WP-редактора зображень
    re.compile(r"@\d+x$"),  # retina
)
# WP додає `-1`, `-2` до назви при повторному завантаженні того самого імені —
# це **справжня** назва вкладення, тож не знімаємо її з основного фрагмента,
# лише пропонуємо коротший fallback.
_DUPLICATE_COUNTER = re.compile(r"-(\d{1,2})$")


class UrlKind(StrEnum):
    """Звідки взялася картинка — від цього залежить, чи її взагалі шукати."""

    uploads = "uploads"
    theme_asset = "theme_asset"
    plugin_asset = "plugin_asset"
    external = "external"
    unknown = "unknown"


@dataclass(frozen=True)
class DerivedSearchKey:
    """Результат деривації одного URL. Незмінний, серіалізовний, без мережі."""

    image_url: str
    search_key: str
    search_key_fallback: str | None
    url_kind: UrlKind
    filename: str
    extension: str | None
    stripped: tuple[str, ...]
    warnings: tuple[str, ...]

    @property
    def is_searchable(self) -> bool:
        """Чи має сенс шукати цей фрагмент у медіатеці."""
        return is_searchable(self.url_kind, self.search_key)


def is_searchable(url_kind: str, search_key: str) -> bool:
    """Єдине місце правила «цей фрагмент варто шукати в медіатеці».

    Живе поза дата-класом, бо той самий висновок треба зробити й для рядка,
    прочитаного з БД (де є лише рядкові поля, не `DerivedSearchKey`).
    """
    return url_kind == UrlKind.uploads.value and len(search_key) >= _MIN_USEFUL_KEY_LENGTH


def strip_wp_suffixes(stem: str) -> tuple[str, list[str]]:
    """Зняти всі похідні суфікси WP/resize-плагінів. Ідемпотентна.

    Повертає `(очищений stem, що саме знято)`. Ніколи не з'їдає весь рядок:
    якщо після зняття лишилось би порожньо — суфікс лишається на місці, бо
    така «назва» не допоможе шукати.
    """
    stripped: list[str] = []
    current = stem
    changed = True
    while changed:
        changed = False
        for pattern in _SUFFIX_PATTERNS:
            match = pattern.search(current)
            if match is None:
                continue
            candidate = current[: match.start()]
            if not candidate:
                continue
            stripped.append(match.group(0))
            current = candidate
            changed = True
    return current, stripped


def _unwrap_proxies(url: str, warnings: list[str], depth: int = 0) -> tuple[str, str, str]:
    """Розгорнути CDN/resize-обгортку до справжнього хоста і шляху.

    Повертає `(scheme, host, path)` уже percent-decoded по шляху.
    """
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    host = parts.hostname or ""
    path = parts.path

    # Проксі з цільовим URL у query (`?url=https://site.com/...`).
    if depth < 2 and parts.query:
        query = parse_qs(parts.query, keep_blank_values=False)
        for key in _URL_PARAM_KEYS:
            values = query.get(key)
            if not values:
                continue
            inner = unquote(values[0])
            if inner.startswith(("http://", "https://", "//")):
                warnings.append("proxy_url_param_unwrapped")
                return _unwrap_proxies(inner, warnings, depth + 1)
            # weserv і подібні приймають цільову картинку без схеми
            # (`?url=site.com/wp-content/...`) — дописуємо її, щоб urlsplit
            # не прочитав усе як шлях.
            head = inner.split("/", 1)[0]
            if "/" in inner and "." in head:
                warnings.append("proxy_url_param_unwrapped")
                return _unwrap_proxies(f"https://{inner}", warnings, depth + 1)

    if parts.query and any(key in _RESIZE_QUERY_KEYS for key in parse_qs(parts.query)):
        warnings.append("resize_query_dropped")

    # Jetpack/Photon: `i0.wp.com/<host>/<path>` (схема цілі — у query `ssl=1`).
    if _PHOTON_HOST.match(host) and path.count("/") >= 2:
        segments = path.lstrip("/").split("/", 1)
        if len(segments) == 2 and "." in segments[0]:
            warnings.append("photon_proxy_unwrapped")
            host, path = segments[0], f"/{segments[1]}"
    # Statically: `cdn.statically.io/img/<host>/<path>`.
    elif _STATICALLY_HOST.match(host) and _STATICALLY_PREFIX.match(path):
        rest = _STATICALLY_PREFIX.sub("/", path).lstrip("/").split("/", 1)
        if len(rest) == 2 and "." in rest[0]:
            warnings.append("statically_proxy_unwrapped")
            host, path = rest[0], f"/{rest[1]}"

    # Cloudflare Image Resizing: `/cdn-cgi/image/width=300,quality=80/<path>`.
    if _CLOUDFLARE_PREFIX.match(path):
        warnings.append("cloudflare_resize_unwrapped")
        path = _CLOUDFLARE_PREFIX.sub("/", path)

    decoded = unquote(path)
    # WebP-Express та подібні дублюють uploads-шлях під власним префіксом
    # (`/wp-content/webp-express/doc-root/wp-content/uploads/...`) — справжній
    # шлях завжди починається від ОСТАННЬОГО входження маркера.
    last = decoded.rfind(_UPLOADS_MARKER)
    if last > 0:
        warnings.append("nested_uploads_path_unwrapped")
        decoded = decoded[last:]
    return scheme, host.lower(), decoded


def _split_extension(filename: str, stripped: list[str]) -> tuple[str, str | None]:
    """Відрізати розширення, врахувавши подвійне (`hero.jpg.webp`)."""
    stem, extension = filename, None
    head, dot, tail = stem.rpartition(".")
    if dot and head and tail.lower() in IMAGE_EXTENSIONS:
        inner_head, inner_dot, inner_tail = head.rpartition(".")
        if inner_dot and inner_head and inner_tail.lower() in IMAGE_EXTENSIONS:
            # `hero.jpg.webp` → конвертер приклеїв друге розширення поверх
            # справжнього; шукати треба по `hero`.
            stripped.append(f".{tail}")
            stem, extension = inner_head, inner_tail.lower()
        else:
            stem, extension = head, tail.lower()
    return stem, extension


def _classify(
    scheme: str, host: str, path: str, site_host: str | None
) -> tuple[UrlKind, list[str]]:
    warnings: list[str] = []
    if scheme and scheme not in {"http", "https"}:
        return UrlKind.unknown, ["unsupported_scheme"]

    lowered = path.lower()
    kind: UrlKind | None = None
    if _UPLOADS_MARKER in lowered:
        kind = UrlKind.uploads
    elif "/wp-content/themes/" in lowered:
        kind = UrlKind.theme_asset
    elif "/wp-content/plugins/" in lowered or "/wp-content/mu-plugins/" in lowered:
        kind = UrlKind.plugin_asset

    foreign = bool(host and site_host and not _same_site(host, site_host))
    if kind is None:
        if foreign:
            return UrlKind.external, warnings
        warnings.append("not_a_media_library_path")
        return UrlKind.unknown, warnings
    if foreign:
        # Шлях медіатеки, але чужий хост: фрагмент лишається валідним для того
        # WP, якому файл належить — просто попереджаємо, що це не наш сайт.
        warnings.append("external_host")
    return kind, warnings


def _same_site(host: str, site_host: str) -> bool:
    left = host.removeprefix("www.")
    right = site_host.strip().lower().removeprefix("www.")
    return left == right or left.endswith(f".{right}")


def derive_search_key(raw_url: str, *, site_host: str | None = None) -> DerivedSearchKey:
    """Вивести з URL картинки фрагмент для пошуку в медіатеці WordPress.

    `site_host` (домен клієнта, якщо звіт до нього привʼязаний) потрібен лише
    для класифікації «свій/чужий хост» — на сам фрагмент він не впливає.
    """
    original = raw_url.strip()
    if not original:
        return DerivedSearchKey(
            image_url=raw_url,
            search_key="",
            search_key_fallback=None,
            url_kind=UrlKind.unknown,
            filename="",
            extension=None,
            stripped=(),
            warnings=("empty_url",),
        )

    warnings: list[str] = []
    stripped: list[str] = []
    scheme, host, path = _unwrap_proxies(original, warnings)
    kind, kind_warnings = _classify(scheme, host, path, site_host)
    warnings.extend(kind_warnings)

    filename = path.rsplit("/", 1)[-1] if path else ""
    # `data:`/`ftp:` та інші не-HTTP URL не мають файлу в медіатеці — «шлях»
    # у них не шлях, тож жодного фрагмента з нього не виводимо.
    if "unsupported_scheme" in kind_warnings:
        filename = ""
    if not filename:
        warnings.append("no_filename_in_url")
        return DerivedSearchKey(
            image_url=raw_url,
            search_key="",
            search_key_fallback=None,
            url_kind=UrlKind.unknown if kind is UrlKind.unknown else kind,
            filename="",
            extension=None,
            stripped=tuple(stripped),
            warnings=tuple(dict.fromkeys(warnings)),
        )

    stem, extension = _split_extension(filename, stripped)
    if extension is None:
        warnings.append("no_image_extension")

    stem, suffix_stripped = strip_wp_suffixes(stem)
    stripped.extend(suffix_stripped)

    fallback: str | None = None
    counter = _DUPLICATE_COUNTER.search(stem)
    if counter is not None:
        candidate = stem[: counter.start()]
        if len(candidate) >= _MIN_FALLBACK_LENGTH:
            fallback = candidate
            warnings.append("possible_duplicate_counter")

    if len(stem) > MAX_SEARCH_KEY_LENGTH:
        stem = stem[:MAX_SEARCH_KEY_LENGTH]
        warnings.append("search_key_truncated")
    if fallback is not None and len(fallback) > MAX_SEARCH_KEY_LENGTH:
        fallback = fallback[:MAX_SEARCH_KEY_LENGTH]
    if stem and len(stem) < _MIN_USEFUL_KEY_LENGTH:
        warnings.append("search_key_too_short")

    return DerivedSearchKey(
        image_url=raw_url,
        search_key=stem,
        search_key_fallback=fallback,
        url_kind=kind,
        filename=filename,
        extension=extension,
        stripped=tuple(stripped),
        # dict.fromkeys: дедуплікація зі збереженням порядку — рекурсивне
        # розгортання проксі може додати те саме попередження двічі.
        warnings=tuple(dict.fromkeys(warnings)),
    )
