"""Google Doc (page/blog-фреймворк, вкладки `Framework`/`Content`) ->
`ContentItem` (task-90 §1.4/§2.3/§4.3).

Дві живі форми одного документа, обидві заміряні (task-90 §1.4a) на реальних
файлах, не припущені:

- **Вкладка `Framework`** — бриф для копірайтера. Поля живуть у
  2-колонкових **ТАБЛИЦЯХ** (лейбл | значення), не в послідовності абзаців,
  як у DOCX-варіанті (`readers/docx.py`) — Docs-редактор рендерить той самий
  шаблон таблицями, не плоским текстом. `body` лишається `None` тим самим
  рішенням, що в DOCX-ридері: секція структури — це інструкція копірайтеру
  (`"H2: ... [80–110 слів, поясни..."`), не готова проза.

- **Вкладка `Content`** — готова, опублікована проза. **Заголовки
  закодовані ЛІТЕРАЛЬНИМИ HTML-тегами `<h1>`/`<h2>`/`<h3>` всередині
  звичайних `NORMAL_TEXT` абзаців, не нативними стилями Google Docs** —
  заміряно живо на 4 незалежних документах (SWM/SAPHNY/ALPHA/HWELL): 100%
  абзаців-заголовків несуть тег, 0% використовують `HEADING_*`
  `namedStyleType`. Копірайтери вставляють готовий HTML-вивід як є. Нативний
  `namedStyleType` перевіряється теж, запасним шляхом — раптом інший клієнт
  колись віддасть справжні стилі Google Docs.

Два різних живих шаблони брифу (task-90 §1.4a: ~5300 і ~1420 символів
`Framework`) використовують різні підписи для секції структури сторінки
(`"Structure / Content:"` проти `"Structure, content"`) і різний набір
додаткових рядків (`"Interactive Elements"`, `"FAQ"`, `"Core Information"`,
`"Top Competitor Examples"`). `_LABELS` розпізнає обидва варіанти написання
для полів, які справді потрібні QA (title/description/h1/keywords/links/
структура); нерозпізнаний підпис просто ігнорується — та сама поведінка, що
`readers/docx.py`.

`_HEADING_LINE_RE` (розбір `"H2: ..."`-рядків усередині значення секції
структури) продубльовано з `readers/docx.py`, не імпортовано: два модулі
читають різні типи документів і належать різним таскам (task-53 vs
task-90) — дублювання однієї регулярки дешевше за міжтасковий імпорт
приватного імені чужого owned-модуля.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from app.contentqa.model import ContentDoc, ContentItem
from app.contentqa.sections import ContentSection, chunk_into_sections
from app.integrations.google_api.docs_client import DocTab

_HEADING_LINE_RE = re.compile(r"^\s*(H[123])\s*:\s*(.+?)\s*$", re.IGNORECASE)
_HTML_HEADING_RE = re.compile(r"^<h([1-4])>(.*)</h\1>$", re.IGNORECASE)
_NO_VALUE = {"no", "none", "n/a", "-"}

# {нормалізований лейбл клітинки: тег поля} — покриває обидва живі шаблони
# (task-90 §1.4a). Лейбл нормалізується через `_normalize_label` (whitespace
# зʼїдено, дужковий хвіст обрізано), тому написання з різним регістром/
# крапкою в кінці збігаються автоматично.
_LABELS: dict[str, str] = {
    "url": "url",
    "search intent": "intent",
    "target keywords": "keywords",
    "meta page title:": "title", "meta page title": "title",
    "meta description:": "description", "meta description": "description",
    "h1": "h1",
    "structure / content:": "structure", "structure / content": "structure",
    "structure, content": "structure", "structure content": "structure",
    "internal link + anchor text": "internal_links",
    "external link + anchor text": "external_links", "external link": "external_links",
}

_FRAME_TAB_NAMES = {"framework", "frame", "page framework", "brief"}
_COPY_TAB_NAMES = {"content", "copy", "copywriting"}


def _paragraph_text(el: dict[str, Any]) -> str:
    p = el.get("paragraph") or {}
    return "".join((r.get("textRun") or {}).get("content", "") for r in p.get("elements", []) or [])


def _paragraph_style(el: dict[str, Any]) -> str | None:
    style = ((el.get("paragraph") or {}).get("paragraphStyle") or {}).get("namedStyleType")
    return style if isinstance(style, str) else None


def _normalize_label(text: str) -> str:
    t = re.sub(r"\s+", " ", text or "").strip().lower()
    return re.sub(r"\(.*?\)", "", t).strip()


def _cell_paragraphs(cell: dict[str, Any]) -> list[str]:
    return [_paragraph_text(e) for e in cell.get("content", []) or [] if "paragraph" in e]


def _cell_label(cell: dict[str, Any]) -> str:
    """Лейбл — лише ПЕРШИЙ абзац клітинки, нормалізований (`\\x0b` -> пробіл
    через `\\s+`). Другий абзац у клітинці-лейблі — завжди пояснювальна
    приписка в дужках («Search Intent» + «(guide, list, comparison...)»),
    свідомо відкидається."""
    paras = _cell_paragraphs(cell)
    return _normalize_label(paras[0]) if paras else ""


def _cell_value_lines(cell: dict[str, Any]) -> list[str]:
    """Кожен абзац клітинки-значення -> один чи кілька рядків. Розбиття на
    `\\x0b` (м'який перенос СЕРЕДИНИ значення) — живий приклад: три анкори
    `"Dental Implants\\x0bDental Crowns\\x0bContact Us"` в одній клітинці
    одним абзацом, без окремих параграфів на кожен."""
    lines: list[str] = []
    for para in _cell_paragraphs(cell):
        for segment in para.rstrip("\n").split("\x0b"):
            segment = segment.strip()
            if segment:
                lines.append(segment)
    return lines


def _link_lines(lines: list[str]) -> tuple[str, ...]:
    return tuple(line for line in lines if line.lower() not in _NO_VALUE)


def _collect_table_fields(tab: DocTab) -> dict[str, list[str]]:
    """Кожна таблиця вкладки `Framework` -> `{тег поля: [рядки значення]}`.
    Нерозпізнаний лейбл ігнорується — та сама поведінка, що DOCX-ридер."""
    fields: dict[str, list[str]] = {}
    for el in tab.body_content:
        table = el.get("table")
        if not table:
            continue
        for row in table.get("tableRows", []) or []:
            cells = row.get("tableCells", []) or []
            if len(cells) < 2:
                continue
            tag = _LABELS.get(_cell_label(cells[0]))
            if tag is None:
                continue
            fields.setdefault(tag, []).extend(_cell_value_lines(cells[1]))
    return fields


def _parse_structure_headings(lines: list[str]) -> tuple[tuple[str, str], ...]:
    out: list[tuple[str, str]] = []
    for line in lines:
        m = _HEADING_LINE_RE.match(line)
        if m:
            out.append((m.group(1).upper(), m.group(2)))
    return tuple(out)


def _first(fields: dict[str, list[str]], tag: str) -> str | None:
    """`None` — поля немає в жодній таблиці; `""` — поле є, значення
    порожнє (PLAN §12: два стани ніколи не підмінюють одне одного)."""
    lines = fields.get(tag)
    if lines is None:
        return None
    return lines[0] if lines else ""


def _tab_ref(source: str, tab: DocTab, *, default_title: str) -> str:
    path = tab.path if tab.path and tab.path != (default_title,) else ()
    return f"{source} › {' › '.join(path)}" if path else source


def read_framework_tab(tab: DocTab, *, source: str) -> ContentItem:
    """Одна вкладка `Framework` -> один `ContentItem`. `body=None` завжди —
    та сама причина, що DOCX-ридер: секція структури — інструкція
    копірайтеру, не готова проза (task-53 §2.5 / task-90 §1.4)."""
    fields = _collect_table_fields(tab)
    return ContentItem(
        ref=_tab_ref(source, tab, default_title="Framework"),
        url=_first(fields, "url"),
        intent=_first(fields, "intent"),
        title=_first(fields, "title"),
        description=_first(fields, "description"),
        h1=_first(fields, "h1"),
        headings=_parse_structure_headings(fields.get("structure", [])),
        keywords=tuple(fields.get("keywords", [])),
        internal_links=_link_lines(fields.get("internal_links", [])),
        external_links=_link_lines(fields.get("external_links", [])),
        body=None,
        raw={tag: "\n".join(lines) for tag, lines in fields.items()},
    )


def _copy_paragraph_stream(tab: DocTab) -> list[tuple[str | None, str]]:
    """Кожен непорожній абзац вкладки `Content` -> `(рівень, текст)`:
    `рівень` заданий ("H1".."H4"), коли абзац сам є заголовком (літеральний
    HTML-тег — основний живий шлях — або нативний стиль, запасний); `None`,
    коли це проза. Спільний потік для `read_copy_tab` (плаский `body`+
    `headings`) і `read_copy_sections` (`contentqa/sections.py:
    chunk_into_sections`, task-90 §4.4) — один прохід по документу вирішує,
    що є заголовком, обидва споживачі лише групують інакше."""
    stream: list[tuple[str | None, str]] = []
    for el in tab.body_content:
        if "paragraph" not in el:
            continue
        text = _paragraph_text(el).rstrip("\n").strip()
        if not text:
            continue

        html_match = _HTML_HEADING_RE.match(text)
        if html_match:
            stream.append((f"H{html_match.group(1)}", html_match.group(2).strip()))
            continue

        style = _paragraph_style(el)
        if style and style.startswith("HEADING_"):
            level = style.removeprefix("HEADING_")
            if level.isdigit() and 1 <= int(level) <= 4:
                stream.append((f"H{level}", text))
                continue

        stream.append((None, text))
    return stream


def read_copy_tab(tab: DocTab, *, source: str) -> ContentItem | None:
    """Одна вкладка `Content` (готова проза) -> один `ContentItem` із
    заповненим `body`. `None`, коли вкладка не має жодного непорожнього
    абзацу — поріг "занадто коротка, щоб перевіряти" (task-90 §2.3) вирішує
    викликач (`contentqa/package.py`), тут лише чесний факт "нічого нема"."""
    stream = _copy_paragraph_stream(tab)
    if not stream:
        return None

    headings = [(level, text) for level, text in stream if level is not None]
    body_parts = [text for level, text in stream if level is None]
    h1 = next((text for level, text in headings if level == "H1"), None)

    return ContentItem(
        ref=_tab_ref(source, tab, default_title="Content"),
        h1=h1,
        headings=tuple(headings),
        body="\n\n".join(body_parts) or None,
        raw={},
    )


def read_copy_sections(tab: DocTab) -> tuple[ContentSection, ...]:
    """Ті самі абзаци, що `read_copy_tab`, згруповані заголовок -> проза до
    наступного заголовка (task-90 §4.4/§3 лейн J: Copy ↔ живий сайт,
    порівняння секція-в-секцію, а не diff усієї сторінки)."""
    return chunk_into_sections(_copy_paragraph_stream(tab))


@dataclass(frozen=True)
class ResolvedTabs:
    """Три заміряні стани документа (task-90 §2.3): `frame_doc`/`copy_doc`
    можуть бути `None`, і це не помилка — 22 з 40 живих документів не мають
    вкладки з копією. `frame_tab_title`/`copy_tab_title` — фактична назва
    вкладки, для тексту `not_checked`/`info`-findings в UI, а не мовчазний
    `None`."""

    frame_doc: ContentDoc | None
    copy_doc: ContentDoc | None
    frame_tab_title: str | None
    copy_tab_title: str | None
    unnamed_single_tab: bool  # документ мав рівно одну безіменну вкладку ("Tab 1")


def read_framework_document(tabs: tuple[DocTab, ...], *, source: str) -> ResolvedTabs:
    """Знаходить вкладки `Framework`/`Content` за нормалізованою назвою
    (§2.3, не за індексом — 11 із 40 живих документів мають лише дефолтну
    `Tab 1`). Документ з рівно однією безіменною вкладкою читається як
    `Framework` + `info`-фіндинг про це (викликач будує фіндинг з
    `unnamed_single_tab`)."""
    frame_tab = next((t for t in tabs if _normalize_label(t.title) in _FRAME_TAB_NAMES), None)
    copy_tab = next((t for t in tabs if _normalize_label(t.title) in _COPY_TAB_NAMES), None)

    unnamed_single = False
    if frame_tab is None and copy_tab is None and len(tabs) == 1:
        frame_tab = tabs[0]
        unnamed_single = True

    frame_doc = None
    if frame_tab is not None:
        item = read_framework_tab(frame_tab, source=source)
        frame_doc = ContentDoc(source=source, doc_type="framework_gdoc", items=(item,))

    copy_doc = None
    if copy_tab is not None:
        copy_item = read_copy_tab(copy_tab, source=source)
        if copy_item is not None:
            copy_doc = ContentDoc(source=source, doc_type="copy_gdoc", items=(copy_item,))

    return ResolvedTabs(
        frame_doc=frame_doc, copy_doc=copy_doc,
        frame_tab_title=frame_tab.title if frame_tab is not None else None,
        copy_tab_title=copy_tab.title if copy_tab is not None else None,
        unnamed_single_tab=unnamed_single,
    )
