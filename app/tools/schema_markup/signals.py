"""Мінімальний HTML-сигнал-екстрактор для генератора (task-56 §1): title,
canonical, h1, og:image, og:site_name, article:published_time,
article:modified_time — рівно ті факти, з яких `generate.py` будує
`PageFacts`.

Навмисно **не** перевикористовує `app.monitor.page` — та зона належить
task-35/50/51 (CLAUDE.md: чужі owned paths read-only), і її `_PageParser`
приватний і взагалі не витягує `og:*`/`article:*`. Той самий stdlib-підхід
(`html.parser`), окремий маленький парсер під наш вужчий набір сигналів.
"""

from __future__ import annotations

from dataclasses import dataclass
from html.parser import HTMLParser


@dataclass(frozen=True)
class PageSignals:
    title: str | None = None
    canonical: str | None = None
    h1: str | None = None
    og_image: str | None = None
    og_site_name: str | None = None
    article_published_time: str | None = None
    article_modified_time: str | None = None


class _SignalParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title: str | None = None
        self.canonical: str | None = None
        self.h1: str | None = None
        self.og_image: str | None = None
        self.og_site_name: str | None = None
        self.article_published_time: str | None = None
        self.article_modified_time: str | None = None
        self._in_title = False
        self._in_h1 = False
        self._h1_captured = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        attrs_dict = {k.lower(): (v or "") for k, v in attrs}
        if tag == "title" and self.title is None:
            self._in_title = True
        elif tag == "h1" and not self._h1_captured:
            self._in_h1 = True
        elif tag == "link" and attrs_dict.get("rel", "").strip().lower() == "canonical":
            self.canonical = attrs_dict.get("href", "").strip() or None
        elif tag == "meta":
            prop = attrs_dict.get("property", "").strip().lower()
            content = attrs_dict.get("content", "").strip()
            if not content:
                return
            if prop == "og:image" and self.og_image is None:
                self.og_image = content
            elif prop == "og:site_name" and self.og_site_name is None:
                self.og_site_name = content
            elif prop == "article:published_time" and self.article_published_time is None:
                self.article_published_time = content
            elif prop == "article:modified_time" and self.article_modified_time is None:
                self.article_modified_time = content

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "title":
            self._in_title = False
        elif tag == "h1" and self._in_h1:
            self._in_h1 = False
            self._h1_captured = True

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title = (self.title or "") + data
        elif self._in_h1:
            self.h1 = (self.h1 or "") + data


def extract_signals(html: str) -> PageSignals:
    parser = _SignalParser()
    parser.feed(html)
    parser.close()
    return PageSignals(
        title=(parser.title or "").strip() or None,
        canonical=parser.canonical,
        h1=(parser.h1 or "").strip() or None,
        og_image=parser.og_image,
        og_site_name=parser.og_site_name,
        article_published_time=parser.article_published_time,
        article_modified_time=parser.article_modified_time,
    )
