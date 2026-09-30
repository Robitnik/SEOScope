"""Рядок стратегії -> готовий лаунчер (task-102 §3.3).

Формат береться дослівно з шапок `PageFramework.md` / `BlogFramework.md`.
Порожнє поле форми лишається порожньою міткою: обидва документи прямо кажуть
«Empty fields → you must discover/construct them», тож порожнє тут — легітимний
стан, на відміну від `URL` у TechAudit. Блокує рядок лише те, без чого запуск
неможливий: `CLIENT` і `INPUT`. Жодне значення не вигадується (зокрема
`Commercial destination` — колонки немає, значить порожньо).

Значення з переносами рядків (`Comments` із `WHY:`/`GOAL:`) ідуть під своєю
міткою з відступом: без нього рядок `WHY: …` читається як нове поле форми.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from urllib.parse import urlparse

from app.prompts.registry import PromptTemplate
from app.prompts.strategy_rows import ArticleRow, PageRow, ParsedSheet

PAGE_PREFILLABLE = (
    "Primary Key",
    "Secondary Key",
    "Longtail Key",
    "H1",
    "Title",
    "Description",
    "Breadcrumbs",
    "Comments",
)
BLOG_PREFILLABLE = (
    "Article short description",
    "Keywords",
    "Commercial destination",
    "Title",
    "Description",
    "Anchors",
    "Comments",
)

CLIENT_UNRESOLVED = "client is not resolved — pick one"
_SLUG_MAX = 60


@dataclass(frozen=True)
class RenderedRow:
    row_key: str
    ref: str
    title: str
    mode: str | None  # "existing" | "new" для сторінок; `None` для статей
    url: str | None
    primary_keyword: str | None
    prefilled: list[str]
    has_framework: bool
    framework_link: str | None
    warnings: list[str] = field(default_factory=list)
    rendered: str | None = None
    blocked_reason: str | None = None


def slugify(text: str, *, max_len: int = _SLUG_MAX) -> str:
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_text.lower()).strip("-")
    return slug[:max_len].strip("-")


def _url_slug(url: str) -> str:
    segments = [s for s in urlparse(url).path.split("/") if s]
    return slugify("-".join(segments)) or "home"


def _format_field(label: str, value: str | list[str] | None) -> str:
    if value is None or value == "" or value == []:
        return f"{label}:"
    lines = value if isinstance(value, list) else value.split("\n")
    if len(lines) == 1:
        return f"{label}: {lines[0]}"
    return f"{label}:\n" + "\n".join(f"  {line}" if line else "" for line in lines)


def _head(doc_host_path: str, mode: str, inp: str, client: str, out_dir: str) -> str:
    return (
        f"Read {doc_host_path} and run it end to end.\n"
        "\n"
        f"MODE:    {mode}\n"
        f"INPUT:   {inp}\n"
        f"CLIENT:  {client}\n"
        f"OUT_DIR: {out_dir}\n"
        "Data:\n"
    )


def _out_dir(client_label: str, slug: str, today: date, suffix: str) -> str:
    return f"out/{slugify(client_label) or 'client'}_{slug}_{today.isoformat()}_{suffix}/"


# ── Сторінка ──────────────────────────────────────────────────────────────────


def _page_comments(row: PageRow) -> str | None:
    # У шапці документа немає поля для цільової адреси, а без неї агент не
    # отримує її взагалі: для нової сторінки й перейменування — окремим рядком
    # у кінці коментаря.
    target = row.new_url if (row.mode == "new" or row.new_url != row.url) else None
    if target is None:
        return row.comments
    line = f"Target URL: {target}"
    return f"{row.comments}\n{line}" if row.comments else line


def _render_page(
    row: PageRow, *, client_label: str, doc_host_path: str, today: date
) -> tuple[str | None, str | None]:
    mode = row.mode
    if mode == "existing":
        if row.url is None:
            return None, "existing page has no live URL"
        inp = row.url
        slug = _url_slug(row.url)
    else:
        inp_text = row.page_name or row.primary
        if inp_text is None:
            return None, "new page has no name or primary keyword"
        inp = inp_text
        slug = slugify(inp_text) or "page"
    fields = [
        _format_field("Primary Key", row.primary),
        _format_field("Secondary Key", row.secondary),
        _format_field("Longtail Key", row.longtail),
        _format_field("H1", row.h1),
        _format_field("Title", row.title),
        _format_field("Description", row.description),
        _format_field("Breadcrumbs", row.breadcrumbs),
        _format_field("Comments", _page_comments(row)),
    ]
    head = _head(
        doc_host_path, mode, inp, client_label, _out_dir(client_label, slug, today, "framework")
    )
    return head + "\n\n".join(fields) + "\n", None


def _page_prefilled(row: PageRow) -> list[str]:
    values = {
        "Primary Key": row.primary,
        "Secondary Key": row.secondary,
        "Longtail Key": row.longtail,
        "H1": row.h1,
        "Title": row.title,
        "Description": row.description,
        "Breadcrumbs": row.breadcrumbs,
        "Comments": _page_comments(row),
    }
    return [label for label in PAGE_PREFILLABLE if values[label] is not None]


# ── Стаття ────────────────────────────────────────────────────────────────────


def _render_article(row: ArticleRow, *, client_label: str, doc_host_path: str, today: date) -> str:
    slug = slugify(row.topic) or "article"
    fields = [
        _format_field("Article Topic", row.topic),
        _format_field("Article short description", row.blurb),
        _format_field("Keywords", ", ".join(row.keywords) or None),
        _format_field("Commercial destination", row.commercial),
        _format_field("Title", row.title),
        _format_field("Description", row.description),
        # По одному на рядок в одному порядку, не CSV: анкор із комою зламав би
        # парування, а документ вимагає «paired in order».
        _format_field("Anchors", list(row.anchors)),
        _format_field("Internal links", list(row.internal_links)),
        _format_field("DO NOT CHANGE", None),
        _format_field("Comments", row.comments),
    ]
    head = _head(
        doc_host_path, "blog", row.topic, client_label, _out_dir(client_label, slug, today, "blog")
    )
    return head + "\n\n".join(fields) + "\n"


def _article_prefilled(row: ArticleRow) -> list[str]:
    values: dict[str, object] = {
        "Article short description": row.blurb,
        "Keywords": row.keywords or None,
        "Commercial destination": row.commercial,
        "Title": row.title,
        "Description": row.description,
        "Anchors": row.anchors or None,
        "Comments": row.comments,
    }
    return [label for label in BLOG_PREFILLABLE if values[label] is not None]


# ── Усі рядки аркуша ──────────────────────────────────────────────────────────


def _unique(key: str, taken: set[str]) -> str:
    candidate, n = key, 2
    while candidate in taken:
        candidate = f"{key}#{n}"
        n += 1
    taken.add(candidate)
    return candidate


def build_rows(
    template: PromptTemplate,
    sheet: ParsedSheet,
    *,
    client_label: str | None,
    doc_host_path: str,
    today: date,
) -> list[RenderedRow]:
    taken: set[str] = set()
    rows: list[RenderedRow] = []
    if template.strategy_role == "page":
        for page in sheet.pages:
            key = _unique(page.url or slugify(page.page_name or page.primary or page.ref), taken)
            rendered: str | None = None
            blocked = CLIENT_UNRESOLVED if client_label is None else None
            if client_label is not None:
                rendered, blocked = _render_page(
                    page, client_label=client_label, doc_host_path=doc_host_path, today=today
                )
            rows.append(
                RenderedRow(
                    row_key=key,
                    ref=page.ref,
                    title=page.page_name or page.primary or page.url or page.ref,
                    mode=page.mode,
                    url=page.url,
                    primary_keyword=page.primary,
                    prefilled=_page_prefilled(page),
                    has_framework=page.framework_text is not None,
                    framework_link=page.framework_link,
                    rendered=rendered,
                    blocked_reason=blocked,
                )
            )
    else:
        for article in sheet.articles:
            key = _unique(slugify(article.topic) or article.ref, taken)
            warnings = []
            if article.dropped_link_pairs:
                n = article.dropped_link_pairs
                warnings.append(f"{n} incomplete link {'pair' if n == 1 else 'pairs'} skipped")
            rendered = None
            blocked = CLIENT_UNRESOLVED if client_label is None else None
            if client_label is not None:
                rendered = _render_article(
                    article, client_label=client_label, doc_host_path=doc_host_path, today=today
                )
            rows.append(
                RenderedRow(
                    row_key=key,
                    ref=article.ref,
                    title=article.topic,
                    mode=None,
                    url=None,
                    primary_keyword=article.keywords[0] if article.keywords else None,
                    prefilled=_article_prefilled(article),
                    has_framework=article.framework_text is not None,
                    framework_link=article.framework_link,
                    warnings=warnings,
                    rendered=rendered,
                    blocked_reason=blocked,
                )
            )
    return rows
