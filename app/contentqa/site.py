"""Живий фетч сторінки + власний екстрактор H2-H4/секцій/вихідних посилань,
для лейнів I (Frame ↔ живий сайт) і J (Copy ↔ живий сайт), task-90
§1.7/§2.4/§4.4, і для матриці Стратегія/Фреймворк/Сайт (task-91 §1.3/§2.4).

`app/monitor/**` — **read-only**: `app.monitor.fetcher.fetch()` дає
SSRF-guard + `HostThrottle` + класифікацію антибот-блоку, а
`app.monitor.page.extract_signals()` — title/description/canonical/meta-robots/
h1. Нове поле в `PageSignals` під це не додається (task-90 §1.7): воно живить
фінгерпринтинг і діфи Site Monitor, і зсув там означав би тисячі хибних
`page_changed` в чужому модулі заради потреб цього тула. Власний екстрактор
H2-H4, прози-по-секціях (`chunk_into_sections`, `contentqa/sections.py`) і
вихідних посилань живе тут — той самий стдлібний `html.parser`, що й
`app.monitor.page`, для консистентності стилю, без спільного коду з ним.
"""

from __future__ import annotations

from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

from app.contentqa.sections import ContentSection, chunk_into_sections
from app.monitor.fetcher import FetchResult, FetchStats, fetch
from app.monitor.page import PageSignals, decode_body, extract_signals
from app.tools.http_safety import HostThrottle

# `nav`/`footer` — на відміну від `app.monitor.page._SKIP_TEXT_TAGS`
# (`script`/`style`/`noscript`/`template`) — виключені навмисно, лише тут:
# ця звірка порівнює структуру, яку АВТОРИЗУВАВ копірайтер (Frame/Copy), із
# тим, що видно на сторінці. WordPress/тема часто позначає H2/H3 у сайдбарі
# ("Related Posts", "Categories") чи навігації — це не частина авторського
# контенту сторінки, і рахувати їх як "заголовок, якого нема у Frame" було б
# хибним фіндингом. `app.monitor.page` цього не потребує — вона взагалі не
# витягує H2+.
_HEADING_TAGS = frozenset({"h1", "h2", "h3", "h4"})
_SKIP_TEXT_TAGS = frozenset({"script", "style", "noscript", "template", "nav", "footer"})
# Посилання НЕ виключають nav/footer (task-91 §3.3, живий баг знайдено й
# виправлено: Framework claims "link to /locations/" -> сторінка
# `/menu/` реально має цей лінк, але лише в блоці локацій, теговано як
# `<nav>` — з тим самим `_SKIP_TEXT_TAGS`, що для заголовків, звірка
# "чи заявлене посилання реально на сторінці" (task-90 §10 follow-up
# `site-internal-link-missing`, task-91 §2.3 links-вердикт) хибно казала
# "не знайдено", хоча посилання фізично присутнє. Заголовок у сайдбарі —
# не авторська структура сторінки (`_SKIP_TEXT_TAGS` рація лишається), але
# посилання в nav/footer — це посилання, яке реально можна клікнути; питання
# "чи є на сторінці" не залежить від того, в якому тезі воно лежить.
_LINK_SKIP_TAGS = frozenset({"script", "style", "noscript", "template"})
_LINK_SKIP_SCHEMES = ("javascript:", "mailto:", "tel:")
# Стеля на кількість посилань, що зберігаються (task-91 §4 — "cap на
# кількість у клітинці з лічильником"): деякі сторінки мають сотні
# однакових карток товару/блогу, і без стелі payload одного фетчу роздувся
# б без користі для звірки.
_MAX_LINKS_PER_BUCKET = 60


def _strip_www(host: str) -> str:
    return host[4:] if host.startswith("www.") else host


def _same_site(host_a: str, host_b: str) -> bool:
    return _strip_www(host_a) == _strip_www(host_b)


@dataclass(frozen=True)
class LivePage:
    """Результат одного живого фетчу для звірки з Frame/Copy. `error` —
    та сама честь, що `FetchResult.error` (`None`/`"timeout"`/
    `"connection_error"`/`"blocked"`/`"unsafe_url"`): недоступна сторінка
    дає `error` заповненим і всі інші поля порожніми — виклик
    `contentqa/crosscheck.py` перетворює це на `not_checked`, ніколи не на
    мовчазне "порушень немає" (PLAN §12)."""

    url: str
    status: int | None
    error: str | None
    signals: PageSignals | None
    headings: tuple[tuple[str, str], ...]
    sections: tuple[ContentSection, ...]
    # `X-Robots-Tag` — HTTP-заголовок, не з HTML, тому окремо від `signals`
    # (task-90 §4.5): `crosscheck.py:check_frame_site`'s `site-noindex`
    # потребує його поряд із `signals.meta_robots`, бо або з них може
    # заборонити індексацію (`app.monitor.page.is_indexable`).
    x_robots_tag: str | None = None
    # Вихідні посилання (task-91 §1.3/§2.4) — `"текст → /шлях"`, той самий
    # display-формат, що вже живе у Frame/Copy (`readers/gdoc.py`), щоб
    # матриця могла показати всі три джерела однаково. Абсолютизовані від
    # `final_url` (після редиректів), розділені на internal/external за
    # хостом; посилання з nav/footer виключені тим самим `_skip_depth`, що
    # й заголовки. `()` завжди, коли сторінка недоступна.
    internal_links: tuple[str, ...] = ()
    external_links: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.error is None and self.status is not None


def fetch_page(
    url: str, *, throttle: HostThrottle, stats: FetchStats, timeout: float = 20.0
) -> LivePage:
    """Один SSRF-guarded фетч + повний розбір title/desc/H1 (наявний
    `extract_signals`) і H2-H4/секцій/посилань (власний розбір нижче)."""
    result: FetchResult = fetch(url, throttle=throttle, stats=stats, timeout=timeout)
    if not result.ok or result.body is None:
        return LivePage(
            url=url, status=result.status, error=result.error or "connection_error",
            signals=None, headings=(), sections=(),
        )

    html = decode_body(result.body, content_type=result.headers.get("Content-Type"))
    signals = extract_signals(html)
    base_url = result.final_url or url
    headings, sections, internal_links, external_links = _extract_page_structure(html, base_url)
    return LivePage(
        url=url, status=result.status, error=None,
        signals=signals, headings=headings, sections=sections,
        x_robots_tag=result.headers.get("X-Robots-Tag"),
        internal_links=internal_links, external_links=external_links,
    )


def _extract_page_structure(
    html: str, base_url: str
) -> tuple[
    tuple[tuple[str, str], ...], tuple[ContentSection, ...], tuple[str, ...], tuple[str, ...]
]:
    parser = _HeadingParser(base_url=base_url)
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # noqa: BLE001 - malformed markup: keep whatever parsed so far
        pass
    headings = tuple((level, text) for level, text in parser.stream if level is not None)
    sections = chunk_into_sections(parser.stream)
    internal = tuple(parser.internal_links[:_MAX_LINKS_PER_BUCKET])
    external = tuple(parser.external_links[:_MAX_LINKS_PER_BUCKET])
    return headings, sections, internal, external


class _HeadingParser(HTMLParser):
    """H1-H4 + текст між ними, у document order — той самий потік-формат
    `(рівень | None, текст)`, що `readers/gdoc.py:_copy_paragraph_stream`
    (task-90 §4.4), щоб `chunk_into_sections` була справді одним алгоритмом
    для обох джерел. Заразом (task-91 §1.3/§2.4) збирає `<a href>` у
    `internal_links`/`external_links` — окремим лічильником глибини
    (`_link_skip_depth`), НЕ тим самим `_skip_depth`, що виключає nav/footer
    із заголовків: посилання в nav/footer лишаються реальними, клікабельними
    посиланнями сторінки (живий баг знайдено й виправлено, task-91 §3.3)."""

    def __init__(self, *, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.stream: list[tuple[str | None, str]] = []
        self.internal_links: list[str] = []
        self.external_links: list[str] = []
        self._base_url = base_url
        self._base_host = (urlsplit(base_url).hostname or "").lower()
        self._skip_depth = 0
        self._link_skip_depth = 0
        self._heading_level: str | None = None
        self._heading_buf: list[str] = []
        self._text_buf: list[str] = []
        self._link_href: str | None = None
        self._link_text_buf: list[str] = []
        self._seen_link_targets: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in _SKIP_TEXT_TAGS:
            self._skip_depth += 1
        if tag in _LINK_SKIP_TAGS:
            self._link_skip_depth += 1
        if tag in _HEADING_TAGS and self._skip_depth == 0:
            self._flush_text()
            self._heading_level = f"H{tag[1]}"
            self._heading_buf = []
        if tag == "a" and self._link_skip_depth == 0 and self._link_href is None:
            href = dict(attrs).get("href") or ""
            noise = href.startswith("#") or href.lower().startswith(_LINK_SKIP_SCHEMES)
            if href and not noise:
                self._link_href = href
                self._link_text_buf = []

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in _SKIP_TEXT_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1
        if tag in _LINK_SKIP_TAGS and self._link_skip_depth > 0:
            self._link_skip_depth -= 1
        if tag in _HEADING_TAGS and self._heading_level is not None:
            heading_text = " ".join("".join(self._heading_buf).split())
            if heading_text:
                self.stream.append((self._heading_level, heading_text))
            self._heading_level = None
            self._heading_buf = []
        if tag == "a" and self._link_href is not None:
            self._flush_link()

    def handle_data(self, data: str) -> None:
        # Ця гілка керує лише заголовками/прозою — `_link_href`/
        # `_link_text_buf` живуть окремо нижче, бо `_skip_depth` (nav/footer
        # включно) не має гасити текст посилання (task-91 §3.3).
        if self._skip_depth == 0:
            if self._heading_level is not None:
                self._heading_buf.append(data)
            else:
                self._text_buf.append(data)
        if self._link_href is not None:
            self._link_text_buf.append(data)

    def _flush_text(self) -> None:
        text = " ".join("".join(self._text_buf).split())
        if text:
            self.stream.append((None, text))
        self._text_buf = []

    def _flush_link(self) -> None:
        href, self._link_href = self._link_href, None
        text = " ".join("".join(self._link_text_buf).split())
        self._link_text_buf = []
        if href is None:
            return
        absolute = urljoin(self._base_url, href)
        parts = urlsplit(absolute)
        if parts.scheme not in ("http", "https"):
            return
        target = urlsplit(absolute)._replace(fragment="").geturl()
        if target in self._seen_link_targets:
            return
        self._seen_link_targets.add(target)
        display = f"{text} → {parts.path or '/'}" if text else target
        bucket = (
            self.internal_links
            if _same_site(parts.hostname or "", self._base_host)
            else self.external_links
        )
        bucket.append(display)

    def close(self) -> None:
        super().close()
        self._flush_text()


__all__ = ["LivePage", "fetch_page"]
