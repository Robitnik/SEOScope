"""Живий фетч сторінки + власний екстрактор H2-H4/секцій, для лейнів I
(Frame ↔ живий сайт) і J (Copy ↔ живий сайт), task-90 §1.7/§2.4/§4.4.

`app/monitor/**` — **read-only**: `app.monitor.fetcher.fetch()` дає
SSRF-guard + `HostThrottle` + класифікацію антибот-блоку, а
`app.monitor.page.extract_signals()` — title/description/canonical/meta-robots/
h1. Нове поле в `PageSignals` під це не додається (task-90 §1.7): воно живить
фінгерпринтинг і діфи Site Monitor, і зсув там означав би тисячі хибних
`page_changed` в чужому модулі заради потреб цього тула. Власний екстрактор
H2-H4 і прози-по-секціях (`chunk_into_sections`, `contentqa/sections.py`)
живе тут — той самий стдлібний `html.parser`, що й `app.monitor.page`, для
консистентності стилю, без спільного коду з ним.
"""

from __future__ import annotations

from dataclasses import dataclass
from html.parser import HTMLParser

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

    @property
    def ok(self) -> bool:
        return self.error is None and self.status is not None


def fetch_page(
    url: str, *, throttle: HostThrottle, stats: FetchStats, timeout: float = 20.0
) -> LivePage:
    """Один SSRF-guarded фетч + повний розбір title/desc/H1 (наявний
    `extract_signals`) і H2-H4/секцій (власний розбір нижче)."""
    result: FetchResult = fetch(url, throttle=throttle, stats=stats, timeout=timeout)
    if not result.ok or result.body is None:
        return LivePage(
            url=url, status=result.status, error=result.error or "connection_error",
            signals=None, headings=(), sections=(),
        )

    html = decode_body(result.body, content_type=result.headers.get("Content-Type"))
    signals = extract_signals(html)
    headings, sections = _extract_headings_and_sections(html)
    return LivePage(
        url=url, status=result.status, error=None,
        signals=signals, headings=headings, sections=sections,
        x_robots_tag=result.headers.get("X-Robots-Tag"),
    )


def _extract_headings_and_sections(
    html: str,
) -> tuple[tuple[tuple[str, str], ...], tuple[ContentSection, ...]]:
    parser = _HeadingParser()
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # noqa: BLE001 - malformed markup: keep whatever parsed so far
        pass
    headings = tuple((level, text) for level, text in parser.stream if level is not None)
    return headings, chunk_into_sections(parser.stream)


class _HeadingParser(HTMLParser):
    """H1-H4 + текст між ними, у document order — той самий потік-формат
    `(рівень | None, текст)`, що `readers/gdoc.py:_copy_paragraph_stream`
    (task-90 §4.4), щоб `chunk_into_sections` була справді одним алгоритмом
    для обох джерел."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stream: list[tuple[str | None, str]] = []
        self._skip_depth = 0
        self._heading_level: str | None = None
        self._heading_buf: list[str] = []
        self._text_buf: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in _SKIP_TEXT_TAGS:
            self._skip_depth += 1
            return
        if tag in _HEADING_TAGS and self._skip_depth == 0:
            self._flush_text()
            self._heading_level = f"H{tag[1]}"
            self._heading_buf = []

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in _SKIP_TEXT_TAGS:
            if self._skip_depth > 0:
                self._skip_depth -= 1
            return
        if tag in _HEADING_TAGS and self._heading_level is not None:
            heading_text = " ".join("".join(self._heading_buf).split())
            if heading_text:
                self.stream.append((self._heading_level, heading_text))
            self._heading_level = None
            self._heading_buf = []

    def handle_data(self, data: str) -> None:
        if self._skip_depth != 0:
            return
        if self._heading_level is not None:
            self._heading_buf.append(data)
        else:
            self._text_buf.append(data)

    def _flush_text(self) -> None:
        text = " ".join("".join(self._text_buf).split())
        if text:
            self.stream.append((None, text))
        self._text_buf = []

    def close(self) -> None:
        super().close()
        self._flush_text()


__all__ = ["LivePage", "fetch_page"]
