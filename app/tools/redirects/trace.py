"""Обхід ланцюжка редіректів хоп за хопом (task-29).

**Чому вручну.** `urllib.request.urlopen` (як і будь-який «зручний» клієнт) сам
іде за 3xx і віддає лише фінальну відповідь: `geturl()` покаже, куди прийшли,
але проміжні статуси зникають — а це саме те, що інструмент показує. Тому
редіректи вимкнені (`_NoRedirectHandler`), 3xx повертається як звичайна
відповідь (`_PassthroughErrorProcessor`), а `Location` ми розвʼязуємо самі
через `urljoin` — бо він законно буває відносним (RFC 7231), і саме на цьому
ламаються наївні реалізації.

**Чому GET, а не HEAD.** HEAD дешевший, але частина серверів відповідає на нього
інакше (405, або 200 там, де GET дає 301) — трейс просто брехав би. Тіло
читається **лише** для фінальної відповіді, і лише щоб дістати `title`,
`canonical`, `meta robots` і `meta refresh`; тіла проміжних хопів не читаються
взагалі.

Мережа інжектується (`Fetcher`), тож усі форми ланцюжків із таск-плану покриті
юніт-тестами без жодного сокета.
"""

from __future__ import annotations

import re
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from enum import StrEnum
from html.parser import HTMLParser
from http import HTTPStatus
from urllib.parse import urljoin, urlsplit

from app.tools.http_safety import (
    NO_CACHE_HEADERS,
    USER_AGENT,
    HostThrottle,
    UnsafeUrlError,
    assert_public_http_url,
    build_cache_busted_url,
    safe_host,
)
from app.tools.redirects.input import (
    SUPPORTED_SCHEMES,
    UrlEntry,
    UrlInputError,
    normalize_url,
)

# Ліміти. Кожен хоп — реальний запит до чужого сайту, тож стелі свідомо низькі.
MAX_HOPS_CAP = 25
DEFAULT_MAX_HOPS = 20
REQUEST_TIMEOUT_SECONDS = 10.0
TOTAL_DEADLINE_SECONDS = 25.0
# Тіло читаємо лише для фінальної сторінки й лише заради `<head>`.
MAX_BODY_BYTES = 512 * 1024

REDIRECT_STATUSES = frozenset({300, 301, 302, 303, 307, 308})
PERMANENT_STATUSES = frozenset({301, 308})
# 401/407 — «потрібна авторизація», 403/429 — «нас не пустили»: це не «сторінка
# мертва», і показувати їх як `broken` означало б брехати оператору.
BLOCKED_STATUSES = frozenset({401, 403, 407, 429})

_THROTTLE = HostThrottle()
_JS_REDIRECT = re.compile(
    r"(?:window\.)?location(?:\.href|\.replace|\.assign)?\s*(?:=|\()\s*['\"]", re.IGNORECASE
)
_META_REFRESH_URL = re.compile(r"url\s*=\s*['\"]?([^'\";]+)", re.IGNORECASE)


class HopKind(StrEnum):
    http = "http"
    meta_refresh = "meta_refresh"


class Permanence(StrEnum):
    permanent = "permanent"
    temporary = "temporary"


class Verdict(StrEnum):
    """Окремі стани, а не «ок / не ок» (PLAN §12)."""

    ok_direct = "ok_direct"
    ok_redirect = "ok_redirect"
    chain = "chain"
    loop = "loop"
    too_many_hops = "too_many_hops"
    broken = "broken"
    broken_chain = "broken_chain"
    blocked = "blocked"
    unsupported_scheme = "unsupported_scheme"
    unsafe_url = "unsafe_url"
    fetch_failed = "fetch_failed"


@dataclass(frozen=True)
class TraceOptions:
    max_hops: int = DEFAULT_MAX_HOPS
    follow_meta_refresh: bool = True
    # Вимкнений за замовчуванням, на відміну від alt-verify: cache-buster змінює
    # URL, а частина сайтів редіректить залежно від query — тобто зіпсував би
    # саме те, що ми міряємо. Оператор вмикає його свідомо.
    cache_buster: bool = False
    timeout_seconds: float = REQUEST_TIMEOUT_SECONDS
    total_deadline_seconds: float = TOTAL_DEADLINE_SECONDS

    @classmethod
    def clamped(cls, raw: Mapping[str, object] | None) -> TraceOptions:
        """Опції з запиту, обрізані до безпечних меж."""
        raw = raw or {}
        requested = raw.get("max_hops", DEFAULT_MAX_HOPS)
        max_hops = DEFAULT_MAX_HOPS
        if isinstance(requested, int | float | str):
            try:
                max_hops = int(requested)
            except ValueError:
                max_hops = DEFAULT_MAX_HOPS
        return cls(
            max_hops=max(1, min(MAX_HOPS_CAP, max_hops)),
            follow_meta_refresh=bool(raw.get("follow_meta_refresh", True)),
            cache_buster=bool(raw.get("cache_buster", False)),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "max_hops": self.max_hops,
            "follow_meta_refresh": self.follow_meta_refresh,
            "cache_buster": self.cache_buster,
        }


@dataclass(frozen=True)
class RawResponse:
    """Одна відповідь без жодного слідування за редіректом."""

    status: int
    headers: tuple[tuple[str, str], ...] = ()
    body: str = ""
    truncated: bool = False

    def values(self, name: str) -> list[str]:
        needle = name.casefold()
        return [value for key, value in self.headers if key.casefold() == needle]


@dataclass(frozen=True)
class Hop:
    index: int
    url: str
    status: int
    status_text: str
    method: str = "GET"
    kind: HopKind = HopKind.http
    permanence: Permanence | None = None
    location: str | None = None
    changes: tuple[str, ...] = ()
    elapsed_ms: int | None = None
    notes: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "index": self.index,
            "url": self.url,
            "method": self.method,
            "status": self.status,
            "status_text": self.status_text,
            "kind": self.kind.value,
            "permanence": self.permanence.value if self.permanence else None,
            "location": self.location,
            "changes": list(self.changes),
            "elapsed_ms": self.elapsed_ms,
            "notes": list(self.notes),
        }


@dataclass(frozen=True)
class FinalPage:
    title: str | None = None
    canonical: str | None = None
    meta_robots: str | None = None
    possible_js_redirect: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "title": self.title,
            "canonical": self.canonical,
            "meta_robots": self.meta_robots,
            "possible_js_redirect": self.possible_js_redirect,
        }

    def is_empty(self) -> bool:
        return not (
            self.title or self.canonical or self.meta_robots or self.possible_js_redirect
        )


@dataclass(frozen=True)
class TraceResult:
    input_url: str
    normalized_url: str
    verdict: Verdict
    hops: tuple[Hop, ...] = ()
    final_url: str | None = None
    final_status: int | None = None
    flags: tuple[str, ...] = ()
    final_page: FinalPage | None = None
    error_code: str | None = None
    error_detail: str | None = None
    elapsed_ms: int = 0

    @property
    def hop_count(self) -> int:
        """Кількість **редіректів** (0 = прямий 200), а не довжина списку хопів."""
        return sum(1 for hop in self.hops if hop.location)


Fetcher = Callable[[str], RawResponse]


# --- реальна мережа ------------------------------------------------------------


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Не слідувати за 3xx: ланцюжок ми ведемо самі."""

    def redirect_request(self, *args: object, **kwargs: object) -> None:
        return None


class _PassthroughErrorProcessor(urllib.request.HTTPErrorProcessor):
    """Віддавати 3xx/4xx/5xx як звичайну відповідь, а не як `HTTPError`.

    Інакше кожен хоп ланцюжка приходив би винятком, і `Location` доводилось би
    виколупувати з обʼєкта помилки.
    """

    def http_response(self, request: object, response: object) -> object:
        return response

    https_response = http_response


def _content_type(response: RawResponse) -> str:
    values = response.values("content-type")
    return values[0].split(";", 1)[0].strip().casefold() if values else ""


def fetch_no_redirect(url: str, *, timeout: float = REQUEST_TIMEOUT_SECONDS) -> RawResponse:
    """Один запит без слідування за редіректом. Кидає `urllib.error.*`/`OSError`."""
    assert_public_http_url(url)
    request = urllib.request.Request(  # noqa: S310 - схема перевірена гардом вище
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            **NO_CACHE_HEADERS,
        },
    )
    opener = urllib.request.build_opener(_NoRedirectHandler, _PassthroughErrorProcessor)
    with _THROTTLE.slot(safe_host(url)), opener.open(request, timeout=timeout) as response:
        headers = tuple((key, value) for key, value in response.headers.items())
        status = int(response.status)
        body = ""
        truncated = False
        # Тіло потрібне лише для фінальної сторінки: у 3xx воно порожнє або
        # службове, і читати його — марний трафік із чужого сайту.
        if status not in REDIRECT_STATUSES:
            raw = response.read(MAX_BODY_BYTES + 1)
            truncated = len(raw) > MAX_BODY_BYTES
            charset = response.headers.get_content_charset() or "utf-8"
            body = raw[:MAX_BODY_BYTES].decode(charset, errors="replace")
        return RawResponse(status=status, headers=headers, body=body, truncated=truncated)


# --- фінальна сторінка ---------------------------------------------------------


class _HeadCollector(HTMLParser):
    """Дістати з `<head>` те, що впливає на трактування редіректу."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title: str | None = None
        self.canonical: str | None = None
        self.meta_robots: str | None = None
        self.meta_refresh: str | None = None
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {name.casefold(): (value or "") for name, value in attrs}
        if tag == "title" and self.title is None:
            self._in_title = True
        elif tag == "link":
            rels = values.get("rel", "").casefold().split()
            if "canonical" in rels and self.canonical is None:
                self.canonical = values.get("href", "").strip() or None
        elif tag == "meta":
            name = values.get("name", "").casefold()
            equiv = values.get("http-equiv", "").casefold()
            content = values.get("content", "").strip()
            if name == "robots" and self.meta_robots is None:
                self.meta_robots = content or None
            elif equiv == "refresh" and self.meta_refresh is None:
                found = _META_REFRESH_URL.search(content)
                if found:
                    self.meta_refresh = found.group(1).strip()

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title = ((self.title or "") + data)[:300]


@dataclass
class _ParsedHead:
    page: FinalPage
    meta_refresh: str | None


def _parse_head(html: str) -> _ParsedHead:
    collector = _HeadCollector()
    collector.feed(html)
    collector.close()
    title = (collector.title or "").strip() or None
    return _ParsedHead(
        page=FinalPage(
            title=title,
            canonical=collector.canonical,
            meta_robots=collector.meta_robots,
            possible_js_redirect=bool(_JS_REDIRECT.search(html)),
        ),
        meta_refresh=collector.meta_refresh,
    )


# --- порівняння URL ------------------------------------------------------------


def _key(url: str) -> str:
    """Ключ для детекції петлі: те, що сервер вважає тим самим запитом."""
    parts = urlsplit(url)
    return f"{parts.scheme.casefold()}://{parts.netloc.casefold()}{parts.path}?{parts.query}"


def _bare_host(host: str) -> str:
    host = host.casefold()
    return host[4:] if host.startswith("www.") else host


def same_page(left: str | None, right: str | None) -> bool:
    """Чи це та сама сторінка (без різниці у слеші, регістрі хоста, схемі)."""
    if not left or not right:
        return False
    first, second = urlsplit(left), urlsplit(right)
    if _bare_host(first.hostname or "") != _bare_host(second.hostname or ""):
        return False
    if first.path.rstrip("/") != second.path.rstrip("/"):
        return False
    return first.query == second.query


def describe_change(from_url: str, to_url: str) -> tuple[str, ...]:
    """Що саме змінилося на цьому кроці — словами, а не «URL інший»."""
    source, target = urlsplit(from_url), urlsplit(to_url)
    changes: list[str] = []

    if source.scheme != target.scheme:
        if source.scheme == "http" and target.scheme == "https":
            changes.append("scheme_http_to_https")
        elif source.scheme == "https" and target.scheme == "http":
            changes.append("scheme_https_to_http")
        else:
            changes.append("scheme_change")

    source_host = (source.hostname or "").casefold()
    target_host = (target.hostname or "").casefold()
    if source_host != target_host:
        if _bare_host(source_host) == _bare_host(target_host):
            changes.append("www_added" if target_host.startswith("www.") else "www_removed")
        else:
            changes.append("host_change")
    if source.port != target.port:
        changes.append("port_change")

    if source.path != target.path:
        if source.path.rstrip("/") == target.path.rstrip("/"):
            changes.append(
                "trailing_slash_added"
                if target.path.endswith("/")
                else "trailing_slash_removed"
            )
        elif source.path.casefold() == target.path.casefold():
            changes.append("case_changed")
        else:
            changes.append("path_change")

    if source.query != target.query:
        if source.query and not target.query:
            changes.append("query_dropped")
        elif target.query and not source.query:
            changes.append("query_added")
        else:
            changes.append("query_changed")

    return tuple(changes)


def _status_text(status: int) -> str:
    try:
        return HTTPStatus(status).phrase
    except ValueError:
        return ""


def _is_homepage(url: str) -> bool:
    parts = urlsplit(url)
    return parts.path in {"", "/"} and not parts.query


# --- трейс ---------------------------------------------------------------------


@dataclass
class _State:
    """Мутабельний стан обходу — щоб гілки виходу лишались читабельними."""

    input_url: str
    normalized_url: str
    hops: list[Hop] = field(default_factory=list)
    notes: tuple[str, ...] = ()
    used_cache_buster: bool = False
    started: float = field(default_factory=time.monotonic)

    def elapsed_ms(self) -> int:
        return int((time.monotonic() - self.started) * 1000)


def trace_redirects(
    url: str,
    *,
    options: TraceOptions | None = None,
    fetcher: Fetcher | None = None,
    nonce: str | None = None,
) -> TraceResult:
    """Пройти ланцюжок від `url` до фінальної відповіді.

    Ніколи не кидає на мережевих проблемах: кожна причина — окремий вердикт із
    деталями, бо «не змогли перевірити» і «редіректів немає» — різні речі.

    Якщо ввімкнений cache-buster зіпсував відповідь (частина сайтів віддає 404
    або блокує будь-який невідомий query-параметр), трейс автоматично
    повторюється **без** нього: інакше інструмент показував би 404 там, де
    сторінка насправді жива. Обидві спроби чесно позначені прапорцями.
    """
    options = options or TraceOptions()
    try:
        entry = normalize_url(url)
    except UrlInputError as exc:
        verdict = (
            Verdict.unsupported_scheme
            if exc.code == "unsupported_scheme"
            else Verdict.fetch_failed
        )
        return TraceResult(
            input_url=url.strip()[:2048],
            normalized_url=url.strip()[:2048],
            verdict=verdict,
            error_code=exc.code,
            error_detail=str(exc),
            flags=(),
        )

    fetch = fetcher or (lambda target: fetch_no_redirect(target, timeout=options.timeout_seconds))
    result = _walk(entry, options, fetch, use_cache_buster=options.cache_buster, nonce=nonce)
    if options.cache_buster and result.verdict in _CACHE_BUSTER_SUSPECTS:
        clean = _walk(entry, options, fetch, use_cache_buster=False, nonce=nonce)
        if clean.verdict not in _CACHE_BUSTER_SUSPECTS:
            # Той самий URL без параметра відповів нормально → показуємо його,
            # але кажемо, що cache-buster саме тут і був причиною.
            return replace(clean, flags=(*clean.flags, "cache_buster_broke_the_url"))
    return result


# Вердикти, які cache-buster може створити на порожньому місці: сайт бачить
# невідомий параметр і віддає 404/403 або взагалі відмовляє.
_CACHE_BUSTER_SUSPECTS = frozenset(
    {Verdict.broken, Verdict.blocked, Verdict.fetch_failed, Verdict.broken_chain}
)


def _walk(
    entry: UrlEntry,
    options: TraceOptions,
    fetch: Fetcher,
    *,
    use_cache_buster: bool,
    nonce: str | None,
) -> TraceResult:
    """Один прохід ланцюжка (без fallback-логіки)."""
    state = _State(
        input_url=entry.input_url,
        normalized_url=entry.url,
        notes=entry.notes,
        used_cache_buster=use_cache_buster,
    )
    current = build_cache_busted_url(entry.url, nonce) if use_cache_buster else entry.url
    visited: set[str] = set()

    for _ in range(options.max_hops + 1):
        if time.monotonic() - state.started > options.total_deadline_seconds:
            return _fail(
                state,
                options,
                Verdict.fetch_failed,
                "timeout",
                f"The chain did not finish within {int(options.total_deadline_seconds)} seconds.",
            )

        visited.add(_key(current))
        started_hop = time.monotonic()
        try:
            response = fetch(current)
        except UnsafeUrlError as exc:
            return _fail(state, options, Verdict.unsafe_url, "unsafe_url", str(exc))
        except urllib.error.HTTPError as exc:  # pragma: no cover - passthrough має ловити раніше
            response = RawResponse(status=exc.code, headers=tuple(exc.headers.items()))
        except urllib.error.URLError as exc:
            return _fail(
                state,
                options,
                Verdict.fetch_failed,
                "url_error",
                f"{safe_host(current)} could not be reached: {exc.reason}.",
            )
        except TimeoutError:
            return _fail(
                state,
                options,
                Verdict.fetch_failed,
                "timeout",
                f"{safe_host(current)} did not respond in time.",
            )
        except OSError as exc:
            return _fail(
                state,
                options,
                Verdict.fetch_failed,
                "os_error",
                f"{safe_host(current)} could not be reached: {exc}.",
            )

        elapsed = int((time.monotonic() - started_hop) * 1000)
        status = response.status

        if status in REDIRECT_STATUSES:
            locations = [value.strip() for value in response.values("location") if value.strip()]
            if not locations:
                state.hops.append(
                    Hop(
                        index=len(state.hops) + 1,
                        url=current,
                        status=status,
                        status_text=_status_text(status),
                        elapsed_ms=elapsed,
                        notes=("missing_location",),
                    )
                )
                return _fail(
                    state,
                    options,
                    Verdict.broken_chain,
                    "missing_location",
                    f"The server answered {status} but sent no Location header, "
                    "so the chain has nowhere to go.",
                )
            target = urljoin(current, locations[0])
            notes = ("multiple_location_headers",) if len(locations) > 1 else ()
            target_scheme = urlsplit(target).scheme.casefold()
            hop = Hop(
                index=len(state.hops) + 1,
                url=current,
                status=status,
                status_text=_status_text(status),
                permanence=(
                    Permanence.permanent if status in PERMANENT_STATUSES else Permanence.temporary
                ),
                location=target,
                changes=describe_change(current, target),
                elapsed_ms=elapsed,
                notes=notes,
            )
            state.hops.append(hop)
            if target_scheme not in SUPPORTED_SCHEMES:
                return _fail(
                    state,
                    options,
                    Verdict.unsupported_scheme,
                    "unsupported_scheme",
                    f"The redirect points at “{target_scheme}:”, which cannot be traced.",
                )
            if _key(target) in visited:
                return _fail(
                    state,
                    options,
                    Verdict.loop,
                    "redirect_loop",
                    "The chain comes back to a URL it already visited.",
                )
            current = target
            continue

        # --- фінальна (не-3xx) відповідь
        parsed = _parse_head(response.body) if response.body else None
        meta_target = None
        if (
            parsed is not None
            and parsed.meta_refresh
            and options.follow_meta_refresh
            and status < 400
        ):
            meta_target = urljoin(current, parsed.meta_refresh)

        if meta_target is not None and len(state.hops) < options.max_hops:
            state.hops.append(
                Hop(
                    index=len(state.hops) + 1,
                    url=current,
                    status=status,
                    status_text=_status_text(status),
                    kind=HopKind.meta_refresh,
                    location=meta_target,
                    changes=describe_change(current, meta_target),
                    elapsed_ms=elapsed,
                    notes=("meta_refresh",),
                )
            )
            if urlsplit(meta_target).scheme.casefold() not in SUPPORTED_SCHEMES:
                return _fail(
                    state,
                    options,
                    Verdict.unsupported_scheme,
                    "unsupported_scheme",
                    "The meta refresh points at a scheme that cannot be traced.",
                )
            if _key(meta_target) in visited:
                return _fail(
                    state,
                    options,
                    Verdict.loop,
                    "redirect_loop",
                    "The meta refresh points back to a URL already visited.",
                )
            current = meta_target
            continue

        state.hops.append(
            Hop(
                index=len(state.hops) + 1,
                url=current,
                status=status,
                status_text=_status_text(status),
                elapsed_ms=elapsed,
                notes=("body_truncated",) if response.truncated else (),
            )
        )
        return _finish(state, options, response, parsed)

    return _fail(
        state,
        options,
        Verdict.too_many_hops,
        "too_many_hops",
        f"The chain is longer than {options.max_hops} redirects; it may never settle.",
    )


def _redirect_count(hops: Iterable[Hop]) -> int:
    return sum(1 for hop in hops if hop.location)


def _final_verdict(status: int, redirects: int) -> Verdict:
    if status in BLOCKED_STATUSES:
        return Verdict.blocked
    if status >= 400:
        return Verdict.broken
    if redirects == 0:
        return Verdict.ok_direct
    return Verdict.ok_redirect if redirects == 1 else Verdict.chain


def _finish(
    state: _State, options: TraceOptions, response: RawResponse, parsed: _ParsedHead | None
) -> TraceResult:
    final_hop = state.hops[-1]
    redirects = _redirect_count(state.hops)
    page = parsed.page if parsed is not None and not parsed.page.is_empty() else None
    verdict = _final_verdict(final_hop.status, redirects)
    return TraceResult(
        input_url=state.input_url,
        normalized_url=state.normalized_url,
        verdict=verdict,
        hops=tuple(state.hops),
        final_url=final_hop.url,
        final_status=final_hop.status,
        flags=_flags(state, options, final_url=final_hop.url, page=page),
        final_page=page,
        elapsed_ms=state.elapsed_ms(),
    )


def _fail(
    state: _State,
    options: TraceOptions,
    verdict: Verdict,
    error_code: str,
    detail: str,
) -> TraceResult:
    """Обрив ланцюжка: хопи, які вже пройшли, лишаються видимими."""
    last = state.hops[-1] if state.hops else None
    return TraceResult(
        input_url=state.input_url,
        normalized_url=state.normalized_url,
        verdict=verdict,
        hops=tuple(state.hops),
        # Куди дійшли — важливо навіть при обриві; фінальний статус лишається
        # `None`, бо фінальної відповіді ми не отримали.
        final_url=(last.location or last.url) if last else None,
        final_status=None,
        flags=_flags(state, options, final_url=None, page=None),
        error_code=error_code,
        error_detail=detail[:1000],
        elapsed_ms=state.elapsed_ms(),
    )


def _flags(
    state: _State, options: TraceOptions, *, final_url: str | None, page: FinalPage | None
) -> tuple[str, ...]:
    """Сигнали для людини — не вироки. Порядок стабільний (для тестів і UI)."""
    flags: list[str] = []
    if state.notes:
        flags.append("normalized_input")

    redirect_hops = [hop for hop in state.hops if hop.location]
    permanences = {hop.permanence for hop in redirect_hops if hop.permanence}
    if Permanence.temporary in permanences:
        flags.append("temporary_redirect")
    if len(permanences) > 1:
        flags.append("mixed_permanence")

    all_changes = {change for hop in redirect_hops for change in hop.changes}
    if "scheme_https_to_http" in all_changes:
        flags.append("protocol_downgrade")
    if "host_change" in all_changes:
        flags.append("host_change")
    if {"www_added", "www_removed"} & all_changes:
        flags.append("www_change")
    if "query_dropped" in all_changes:
        flags.append("query_dropped")
    if "case_changed" in all_changes:
        flags.append("case_changed")
    slash_only = {"trailing_slash_added", "trailing_slash_removed"}
    if len(redirect_hops) == 1 and all_changes <= slash_only:
        flags.append("trailing_slash_only")
    if any(hop.kind is HopKind.meta_refresh for hop in state.hops):
        flags.append("meta_refresh_used")
    if any("multiple_location_headers" in hop.notes for hop in state.hops):
        flags.append("multiple_location_headers")

    if (
        redirect_hops
        and final_url
        and _is_homepage(final_url)
        and not _is_homepage(state.normalized_url)
    ):
        flags.append("redirect_to_homepage")

    if page is not None:
        canonical = urljoin(final_url or "", page.canonical) if page.canonical else None
        if canonical and not same_page(canonical, final_url):
            flags.append("canonical_mismatch")
        if page.meta_robots and "noindex" in page.meta_robots.casefold():
            flags.append("noindex_destination")
        if page.possible_js_redirect:
            flags.append("possible_js_redirect")

    if state.used_cache_buster:
        flags.append("cache_buster_used")
    return tuple(flags)
