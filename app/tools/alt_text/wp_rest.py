"""Точний WordPress `attachment ID` через публічний REST медіатеки (task-39).

**Чому це взагалі можливо для `1.jpg`.** `search_key` (task-27) — це голе
імʼя файлу без суфіксів, і для беззмістовних назв воно даремне: пошук по «1»
у медіатеці — це `LIKE '%1%'`, сотні збігів. Але REST-пошук WordPress
(`WP_REST_Attachments_Controller::prepare_items_query()`, WP ≥ 6.0.3) під
`search` вмикає `wp_allow_query_attachment_by_filename`, і `WP_Query` шукає
не лише по заголовку, а й по `postmeta._wp_attached_file` — тобто по
**повному uploads-шляху оригіналу** (`2024/03/1.jpg`). Цей шлях ми маємо з
URL картинки (`wp_media.derive_upload_location`), і він звужує вибірку до
одного вкладення навіть для «1.jpg».

**Порядок пошуку, не більше трьох запитів:**
1. `?search=<original_relative_path>` — точний шлях, найдешевший постріл.
2. якщо порожньо: `?slug=<sanitize_title(stem)>` — WP кладе slug із назви
   файлу при завантаженні.
3. якщо й це порожньо: `?search=<stem>` без каталогу — ширший пошук, лише
   для класифікації (скільки кандидатів, а не «знайшли»).

**Підтвердження, а не довіра до пошуку.** `search` — це `LIKE`, тому кожен
кандидат звіряється локально: `media_details.file` (шлях оригіналу з
відповіді) або будь-який `media_details.sizes[*].file` мусить збігтися з
нашим шляхом/іменем. Один точний збіг → `resolved`. Кілька — `ambiguous`, і
ID **ніколи** не подається як точний (PLAN §12: здогад ≠ факт).

Мережа — stdlib `urllib.request` поверх `app.tools.http_safety` (той самий
SSRF-гард/UA/тротлінг, що й у `verify.py`/Redirect Checker): нових
залежностей немає.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from urllib.parse import parse_qs, quote, urlencode, urlsplit, urlunsplit

from app.tools.alt_text.wp_media import UploadLocation
from app.tools.http_safety import (
    NO_CACHE_HEADERS,
    USER_AGENT,
    GuardedRedirectHandler,
    HostThrottle,
    UnsafeUrlError,
    assert_public_http_url,
    safe_host,
)

__all__ = [
    "MediaCandidate",
    "MediaConfidence",
    "MediaLookup",
    "MediaLookupStatus",
    "RestResponse",
    "build_admin_url",
    "fetch_rest",
    "lookup_media",
]

REQUEST_TIMEOUT_SECONDS = 10.0
MAX_BODY_BYTES = 1024 * 1024
MAX_CANDIDATES = 5

_FIELDS = "id,slug,link,alt_text,source_url,media_details,title"
_THROTTLE = HostThrottle()


class MediaLookupStatus(StrEnum):
    """Стан пошуку. `not_searchable` сюди не входить: цей стан ставить
    API-шар *до* виклику `lookup_media`, без жодного мережевого запиту."""

    resolved = "resolved"
    ambiguous = "ambiguous"
    not_found = "not_found"
    rest_unavailable = "rest_unavailable"
    failed = "failed"


class MediaConfidence(StrEnum):
    """Наскільки певний збіг. Заповнюється лише при `resolved`."""

    exact = "exact"
    filename = "filename"


@dataclass(frozen=True)
class MediaCandidate:
    """Один кандидат зі списку `ambiguous` — вибір лишається за оператором."""

    id: int
    file: str | None
    source_url: str | None
    alt_text: str | None
    title: str | None


@dataclass(frozen=True)
class MediaLookup:
    """Результат одного пошуку. Незмінний, серіалізовний, без побічних ефектів."""

    status: MediaLookupStatus
    media_id: int | None = None
    confidence: MediaConfidence | None = None
    alt_text: str | None = None
    slug: str | None = None
    source_url: str | None = None
    admin_url: str | None = None
    candidates: tuple[MediaCandidate, ...] = ()
    detail: str | None = None


@dataclass(frozen=True)
class RestResponse:
    """Сира відповідь REST-запиту — HTTP-помилки тут теж дані, не виняток,

    щоб `_query` міг однаково класифікувати 200/401/403/404/5xx, не ловлячи
    `HTTPError` окремо для кожного виклику.
    """

    http_status: int
    content_type: str
    body: bytes


Fetcher = Callable[[str], RestResponse]


def build_admin_url(wp_root: str, media_id: int) -> str:
    """`upload.php?item=<ID>` — відкриває медіатеку одразу з модалкою

    «Attachment details» цього вкладення. Рівно той URL, що core сам пише в
    адресний рядок при кліку на файл у сітці
    (`wp-includes/js/media-grid.js`: `this.gridRouter.navigate(... '?item=' + this.model.id)`)
    — **без** `&mode=grid`. Живою перевіркою знайдено чому: Backbone-роутер
    сітки матчить фрагмент патерном `'upload.php?item=:slug'`, а `:slug`
    компілюється в `([^/?]+)` — виключає `/` і `?`, але **не** `&`. Тобто
    `item=<ID>&mode=grid` матчиться, але захоплює як slug увесь хвіст
    `"<ID>&mode=grid"` цілком. Поки вкладення вже є в заздалегідь
    завантаженій сторінці сітки, `parseInt()` це терпить — але коли ні,
    роутер іде довантажувати вкладення через `wp.media.attachment(query)` із
    цим самим зіпсованим рядком як ID, запит провалюється мовчки, і модалка
    просто не відкривається (без жодної помилки в UI — саме це й побачив
    оператор на реальному сайті). Grid — дефолтний режим медіатеки з WP 4.0,
    тож форсувати його `mode=grid` тут не потрібно.
    """
    return f"{wp_root}/wp-admin/upload.php?item={media_id}"


def _search_admin_url(location: UploadLocation) -> str:
    """Фолбек-посилання, коли точний ID не знайдено: те, що оператор і так
    зробив би руками — відкрити медіатеку з пошуком по імені файлу."""
    stem = location.original_basename.rsplit(".", 1)[0]
    return f"{location.wp_root}/wp-admin/upload.php?mode=list&s={quote(stem)}"


def _rest_route_fallback(url: str) -> str | None:
    """`?rest_route=/wp/v2/media&…` — той самий ендпоїнт для plain-permalinks,

    де `/wp-json/…` не має врут-правила. Використовується один раз, лише коли
    прямий шлях віддав 404 з HTML (ознака, що `/wp-json/` не роутиться).
    """
    parts = urlsplit(url)
    marker = "/wp-json/"
    if marker not in parts.path:
        return None
    root, _, rest = parts.path.partition(marker)
    query = parse_qs(parts.query, keep_blank_values=True)
    query["rest_route"] = [f"/{rest}"]
    return urlunsplit((parts.scheme, parts.netloc, f"{root}/", urlencode(query, doseq=True), ""))


def _fetch_once(url: str) -> RestResponse:
    request = urllib.request.Request(  # noqa: S310 - схема перевірена гардом вище
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            **NO_CACHE_HEADERS,
        },
    )
    opener = urllib.request.build_opener(GuardedRedirectHandler)
    try:
        with opener.open(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            raw = response.read(MAX_BODY_BYTES + 1)[:MAX_BODY_BYTES]
            return RestResponse(
                http_status=response.status,
                content_type=response.headers.get_content_type() or "",
                body=raw,
            )
    except urllib.error.HTTPError as exc:
        raw = exc.read(MAX_BODY_BYTES + 1)[:MAX_BODY_BYTES]
        content_type = exc.headers.get_content_type() if exc.headers else ""
        return RestResponse(http_status=exc.code, content_type=content_type or "", body=raw)


def fetch_rest(url: str) -> RestResponse:
    """Запит до REST медіатеки, без кешу, під SSRF-гардом і per-host тротлінгом.

    Один автоматичний фолбек на `?rest_route=`, якщо прямий шлях віддав 404
    з HTML (сайт на plain permalinks).
    """
    assert_public_http_url(url)
    host = safe_host(url)
    with _THROTTLE.slot(host):
        response = _fetch_once(url)
    if response.http_status == 404 and "html" in response.content_type.lower():
        fallback_url = _rest_route_fallback(url)
        if fallback_url is not None:
            assert_public_http_url(fallback_url)
            with _THROTTLE.slot(host):
                response = _fetch_once(fallback_url)
    return response


def _query(
    fetch: Fetcher, wp_root: str, params: dict[str, str]
) -> list[dict[str, Any]] | MediaLookup:
    url = f"{wp_root}/wp-json/wp/v2/media?{urlencode(params)}"
    try:
        response = fetch(url)
    except UnsafeUrlError as exc:
        return MediaLookup(status=MediaLookupStatus.failed, detail=str(exc))
    except urllib.error.URLError as exc:
        return MediaLookup(
            status=MediaLookupStatus.failed, detail=f"Could not reach the site: {exc.reason}."
        )
    except TimeoutError:
        return MediaLookup(
            status=MediaLookupStatus.failed, detail="The site did not respond in time."
        )
    except OSError as exc:
        return MediaLookup(
            status=MediaLookupStatus.failed, detail=f"Could not reach the site: {exc}."
        )

    if response.http_status in (401, 403, 404):
        return MediaLookup(
            status=MediaLookupStatus.rest_unavailable,
            detail=f"WordPress REST API returned HTTP {response.http_status}.",
        )
    if response.http_status != 200:
        return MediaLookup(
            status=MediaLookupStatus.failed,
            detail=f"WordPress REST API returned HTTP {response.http_status}.",
        )
    if "json" not in response.content_type.lower():
        return MediaLookup(
            status=MediaLookupStatus.rest_unavailable,
            detail="The REST API did not return JSON — it may be disabled or blocked.",
        )
    try:
        payload = json.loads(response.body.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        return MediaLookup(
            status=MediaLookupStatus.rest_unavailable,
            detail="The REST API response could not be parsed.",
        )
    if not isinstance(payload, list):
        return MediaLookup(
            status=MediaLookupStatus.rest_unavailable,
            detail="Unexpected REST API response shape.",
        )
    return payload


def _sanitize_title(value: str) -> str:
    """Наближення WP `sanitize_title()` для ASCII назв — достатньо для

    швидкого точного пострілу по `slug`; не-ASCII назви просто не дадуть
    збігу тут і підуть далі, у пошук по кроку 1/3, де це не потрібно.
    """
    result = []
    previous_dash = False
    for char in value.strip().lower():
        if char.isalnum():
            result.append(char)
            previous_dash = False
        elif not previous_dash:
            result.append("-")
            previous_dash = True
    return "".join(result).strip("-")


def _basename_from_url(url: str | None) -> str | None:
    if not url:
        return None
    return urlsplit(url).path.rsplit("/", 1)[-1] or None


def _directory_of(path: str) -> str:
    return path.rsplit("/", 1)[0] if "/" in path else ""


def _is_exact_match(item: dict[str, Any], location: UploadLocation) -> bool:
    details = item.get("media_details") or {}
    item_file = str(details.get("file") or "")
    if item_file == location.original_relative_path:
        return True
    directory = _directory_of(location.original_relative_path)
    item_directory = _directory_of(item_file)
    if directory and item_directory and directory != item_directory:
        # Розмір без каталогу в шляху не можна довіряти без збігу директорії
        # оригіналу — інакше `hero.jpg` в іншому місяці теж пройшов би.
        return False
    sizes = details.get("sizes") or {}
    for size in sizes.values():
        if isinstance(size, dict) and size.get("file") == location.basename:
            return True
    return False


def _is_filename_match(item: dict[str, Any], location: UploadLocation) -> bool:
    """Слабкий збіг — лише коли директорію взагалі нема з чим звірити.

    Якщо `media_details.file` присутній, але веде в іншу директорію, це
    майже завжди **інший файл із тим самим іменем** (типова колізія для
    `1.jpg`), а не той самий, що переїхав на CDN — довіряти тут нічому.
    Слабкий тір існує для вужчого випадку: коли `media_details.file` взагалі
    відсутній у відповіді, і лишається звірити тільки `source_url`.
    """
    details = item.get("media_details") or {}
    if details.get("file"):
        return False
    return _basename_from_url(item.get("source_url")) == location.original_basename


def _best_match(
    items: list[dict[str, Any]], location: UploadLocation
) -> tuple[dict[str, Any], MediaConfidence] | None:
    exact = [item for item in items if _is_exact_match(item, location)]
    if len(exact) == 1:
        return exact[0], MediaConfidence.exact
    if exact:
        return None
    weak = [item for item in items if _is_filename_match(item, location)]
    if len(weak) == 1:
        return weak[0], MediaConfidence.filename
    return None


def _candidate(item: dict[str, Any]) -> MediaCandidate:
    details = item.get("media_details") or {}
    title = item.get("title")
    return MediaCandidate(
        id=int(item["id"]),
        file=details.get("file"),
        source_url=item.get("source_url"),
        alt_text=item.get("alt_text") or None,
        title=(title.get("rendered") if isinstance(title, dict) else title) or None,
    )


def _resolved(
    item: dict[str, Any], location: UploadLocation, confidence: MediaConfidence
) -> MediaLookup:
    media_id = int(item["id"])
    return MediaLookup(
        status=MediaLookupStatus.resolved,
        media_id=media_id,
        confidence=confidence,
        alt_text=item.get("alt_text") or None,
        slug=item.get("slug"),
        source_url=item.get("source_url"),
        admin_url=build_admin_url(location.wp_root, media_id),
    )


def lookup_media(location: UploadLocation, *, fetcher: Fetcher | None = None) -> MediaLookup:
    """Знайти точний `attachment ID` для одного uploads-URL.

    Найбільше три HTTP-запити (див. докстрінг модуля), кожен — до сайту
    клієнта, тож викликається явно за рядок/пакет, ніколи автоматично при
    імпорті звіту.
    """
    fetch = fetcher or fetch_rest

    items = _query(
        fetch,
        location.wp_root,
        {
            "search": location.original_relative_path,
            "media_type": "image",
            "per_page": "20",
            "_fields": _FIELDS,
        },
    )
    if isinstance(items, MediaLookup):
        return items

    if not items:
        slug = _sanitize_title(location.original_basename.rsplit(".", 1)[0])
        if slug:
            slug_items = _query(fetch, location.wp_root, {"slug": slug, "_fields": _FIELDS})
            if isinstance(slug_items, MediaLookup):
                return slug_items
            items = slug_items

    if not items:
        stem = location.original_basename.rsplit(".", 1)[0]
        stem_items = _query(
            fetch,
            location.wp_root,
            {"search": stem, "media_type": "image", "per_page": "100", "_fields": _FIELDS},
        )
        if isinstance(stem_items, MediaLookup):
            return stem_items
        items = stem_items

    if not items:
        return MediaLookup(
            status=MediaLookupStatus.not_found,
            admin_url=_search_admin_url(location),
            detail="No attachment matched this file in the media library.",
        )

    match = _best_match(items, location)
    if match is not None:
        item, confidence = match
        return _resolved(item, location, confidence)

    return MediaLookup(
        status=MediaLookupStatus.ambiguous,
        admin_url=_search_admin_url(location),
        candidates=tuple(_candidate(item) for item in items[:MAX_CANDIDATES]),
        detail=f"{len(items)} attachment(s) matched — pick the right one.",
    )
