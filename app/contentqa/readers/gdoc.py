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

# ── task-92: broadened Copy-tab recognition + block-level stream ───────────
# Live scan of 218 framework documents (task-92 §1.1) found the exact-name
# `_COPY_TAB_NAMES` set below recognizes only 47/96 real copy tabs — the
# rest are named "English Content"/"Swedish Content" (LAXTON, one doc per
# language), "Updated Copy 15/05/26", "<Client> Website Copy", or bare
# "Tab 2" (EFFYS FAQ, second tab, no name at all). `_looks_like_copy_title`
# is the word-boundary fallback; `read_framework_document` also falls back
# structurally (a tab with zero recognized table fields and non-trivial
# content is copy-like regardless of name) for the fully-unnamed case.
_COPY_TAB_WORD_RE = re.compile(r"\b(copy|content)\b")

# Trailing editor instruction, live-measured on 3XE framework docs — the
# form that separates a FINISHED copy tab (EFFYS/LAXTON: 0 markers on
# every tab) from an EDIT BRIEF (3XE: >=2 markers on every tab, task-92
# §1.6). `[^\]]*` lets "[KEEP heading and form]"-style extended markers
# through, not just the bare keyword.
_MARKER_TAIL_RE = re.compile(
    r"\s*\[\s*(?:KEEP|EDIT|NEW|DELETE|REMOVE|AS IS|NO CHANGE)\b[^\]]*\]\s*$", re.IGNORECASE
)
# A live copy paragraph that is layout instruction, not page prose (task-92
# §1.7) — measured forms: bracketed/parenthesized asides
# ("[IMAGE SECTION: ...]", "(EDIT)"), a labelled directive line
# ("Breadcrumbs: Home / ...", "CTA BUTTON: ORDER/DELIVERY"), or a bare
# rule of underscores/dashes used as a visual separator between sections.
_WRAPPED_RE = re.compile(r"^[\[(].+[\])]$", re.DOTALL)
_DIRECTIVE_LABEL_RE = re.compile(
    r"^(image section|image carousel|image|map|jump navigation|breadcrumbs?|"
    r"cta button|cta link|cta|button|developer note|dev note|note|inline|"
    r"eyebrow|tagline|sub-?line|internal link|external link|alt text|schema|"
    r"placement)\s*[:\-–—]",
    re.IGNORECASE,
)
_RULE_LINE_RE = re.compile(r"^[_\-–—=·•*\s]{4,}$")
# Second live form of an HTML heading tag inside Copy prose (task-92 §1.3):
# `_HTML_HEADING_RE` above requires a matching closing tag on the SAME
# line; measured live (EFFYS COLUMBUS_AVE) the opening tag sometimes has
# none at all ("<h2>Effy's Café & Patisserie: Columbus Ave") — Docs lets a
# copywriter forget the close, the paragraph still reads as a heading.
_HTML_HEADING_OPEN_RE = re.compile(r"^<h([1-4])>\s*(.+?)\s*(?:</h[1-4]>)?$", re.IGNORECASE)
# Third live heading form in Copy tabs — a bare "H1: Title" label, the
# same shape `_HEADING_LINE_RE` already parses for Framework's structure
# field, but Copy allows H4 too and lives at paragraph level, not inside a
# structure-field value — kept as its own regex rather than reused, same
# reasoning as the module docstring gives for not sharing regex with
# `readers/docx.py`: different document, different owner.
_COPY_HEADING_LABEL_RE = re.compile(r"^H([1-4])\s*[:.]\s*(.+)$")

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
# Minimum joined-text length for a nameless, fieldless tab to count as a
# copy candidate at all (task-92 §1.1 structural fallback) — well below
# `package.py:MIN_COPY_CHARS` (which decides "checked for QA" vs "not
# written yet"): this threshold only decides "worth listing", a signature
# line or a stray "TBD" note should not become a phantom Copy tab.
_MIN_COPY_CANDIDATE_CHARS = 30


def _looks_like_copy_title(title: str) -> bool:
    """Word-boundary fallback beyond the exact `_COPY_TAB_NAMES` set
    (task-92 §1.1). Underscores/hyphens are folded to spaces first —
    regex `\\b` does not fire on `_` (a word character), so
    `\\bcopy\\b` would otherwise never match inside
    `SAPHNY_COPY_WEBSITE_CLIENT_HOMEPAGE_031224`."""
    folded = re.sub(r"[_\-]+", " ", title or "")
    return bool(_COPY_TAB_WORD_RE.search(_normalize_label(folded)))


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
class DocBlock:
    """One classified `\\x0b`-split chunk of a Copy tab (task-92 §1.3/§2.2)
    — the richer sibling of `_copy_paragraph_stream` above. That function
    is left untouched (still feeds `read_copy_tab`/`read_copy_sections`,
    which lanes D/E/F/J and task-91's matrix already depend on
    byte-for-byte); this one exists only for
    `app.contentqa.copydiff` (task-92)'s sentence-level Copy↔site diff.

    `kind`: `"heading"` | `"directive"` | `"item"` | `"para"`. `text` has
    any trailing `[KEEP]`/`[EDIT]`-style editor marker already stripped;
    `has_marker` records whether one was present — used to tell a
    finished copy tab from an edit brief (task-92 §1.6, measured
    bimodal: 0 markers on every EFFYS/LAXTON tab, >=2 on every 3XE tab)."""

    kind: str
    level: str | None
    text: str
    has_marker: bool


def _strip_marker_tail(text: str) -> tuple[str, bool]:
    stripped = _MARKER_TAIL_RE.sub("", text)
    return (stripped.strip(), True) if stripped != text else (text, False)


def _classify_copy_chunk(text: str, *, style: str | None, bullet: bool) -> DocBlock:
    stripped, has_marker = _strip_marker_tail(text)
    if not stripped or _RULE_LINE_RE.match(stripped):
        return DocBlock(kind="directive", level=None, text=stripped or text, has_marker=has_marker)

    if stripped.lower().startswith("<h"):
        full = _HTML_HEADING_RE.match(stripped)
        if full:
            return DocBlock(
                kind="heading", level=f"H{full.group(1)}", text=full.group(2).strip(),
                has_marker=has_marker,
            )
        opened = _HTML_HEADING_OPEN_RE.match(stripped)
        if opened:
            return DocBlock(
                kind="heading", level=f"H{opened.group(1)}", text=opened.group(2).strip(),
                has_marker=has_marker,
            )

    label = _COPY_HEADING_LABEL_RE.match(stripped)
    if label:
        return DocBlock(
            kind="heading", level=f"H{label.group(1)}", text=label.group(2).strip(),
            has_marker=has_marker,
        )

    if style and style.startswith("HEADING_"):
        level = style.removeprefix("HEADING_")
        if level.isdigit() and 1 <= int(level) <= 4:
            return DocBlock(kind="heading", level=f"H{level}", text=stripped, has_marker=has_marker)

    if _WRAPPED_RE.match(stripped) or _DIRECTIVE_LABEL_RE.match(stripped):
        return DocBlock(kind="directive", level=None, text=stripped, has_marker=has_marker)

    return DocBlock(
        kind="item" if bullet else "para", level=None, text=stripped, has_marker=has_marker
    )


def copy_tab_blocks(tab: DocTab) -> tuple[DocBlock, ...]:
    """Every non-empty block of a Copy tab, classified (task-92 §2.2),
    splitting each paragraph on `\\x0b` first — a live defect
    (`_copy_paragraph_stream` never did this): nine bullet lines crammed
    into one paragraph via soft line breaks
    (`"Real-time EVP scanning\\x0bBuilt-in speaker\\x0b..."`) previously
    read as one run-on "sentence". `paragraph.bullet` is also honored
    here — a list item has no terminal punctuation and must not be split
    into sentences by `copydiff.split_sentences`."""
    out: list[DocBlock] = []
    for el in tab.body_content:
        if "paragraph" not in el:
            continue
        p = el["paragraph"]
        raw = _paragraph_text(el).rstrip("\n")
        bullet = bool(p.get("bullet"))
        style = _paragraph_style(el)
        for chunk in raw.split("\x0b"):
            chunk = " ".join(chunk.split())
            if chunk:
                out.append(_classify_copy_chunk(chunk, style=style, bullet=bullet))
    return tuple(out)


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
    # Ті самі абзаци, що дав `copy_doc.items[0].body`, згруповані по секціях
    # (task-90 §4.6) — лейну J (`crosscheck.py:check_copy_site`) потрібен
    # саме цей розріз, не плаский `body`. `()` коли `copy_doc is None`.
    copy_sections: tuple[ContentSection, ...] = ()
    # УСІ вкладки, що виглядають як копія — не лише та одна, що стала
    # `copy_doc` вище (task-92 §2.1/§2.5): LAXTON лінкує по 2 копі-вкладки
    # на документ (English/Swedish Content), і яка з них "правильна"
    # вирішує ПОКРИТТЯ проти живого сайту (`copydiff.py`), не назва —
    # тому лейн K (`package.py`) прогонить звірку на кожній із них.
    # Порядок документа; `copy_doc`, коли не `None`, завжди присутній тут
    # (перший елемент, якщо `copy_doc` знайдено іменем; інакше перша
    # структурна знахідка).
    all_copy_tabs: tuple[DocTab, ...] = ()
    # `True`, коли `frame_tab` знайдено НЕ за назвою і не через
    # `unnamed_single_tab`, а через фолбек "перша вкладка з розпізнаними
    # полями таблиці" (task-92 §1.1 — 6 живих документів мають Framework,
    # названий просто "Tab 1" поруч з іншими вкладками).
    frame_matched_by_content: bool = False


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
        lone = tabs[0]
        if _collect_table_fields(lone):
            frame_tab = lone
            unnamed_single = True
        else:
            # task-92 §1.1 live defect: a lone unnamed tab is not always a
            # Framework brief in disguise — 19 live documents are a
            # SINGLE tab of finished copy that never got its own name
            # ("Brandons Donuts Website Copy" et al., tab title == doc
            # name). Every measured live Framework tab has >=1 recognized
            # brief field (task-90 §1.4a); zero fields here means this is
            # Copy, not Framework, regardless of tab count.
            copy_tab = lone

    # task-92 §1.1: broaden beyond the exact-name fast path above. A tab
    # counts as copy-like when it is not the frame tab AND carries no
    # recognized brief field (`_collect_table_fields` empty — a
    # Framework's tables always have >=1 recognized field; a Copy tab
    # never has any) AND either its name says so (`_looks_like_copy_title`)
    # or it simply has enough content to be worth checking — the latter
    # catches genuinely unlabeled tabs like EFFYS's bare "Tab 2" (its FAQ
    # copy, live-measured, task-92 §1.1).
    all_copy_tabs: list[DocTab] = []
    seen_ids: set[int] = set()
    if copy_tab is not None:
        all_copy_tabs.append(copy_tab)
        seen_ids.add(id(copy_tab))
    for t in tabs:
        if t is frame_tab or id(t) in seen_ids:
            continue
        if _collect_table_fields(t):
            continue  # has recognized brief fields -> a Framework variant, not copy
        stream = _copy_paragraph_stream(t)
        content_chars = sum(len(text) for _, text in stream)
        if _looks_like_copy_title(t.title) or content_chars >= _MIN_COPY_CANDIDATE_CHARS:
            all_copy_tabs.append(t)
            seen_ids.add(id(t))

    if copy_tab is None and all_copy_tabs:
        copy_tab = all_copy_tabs[0]

    # task-92 §1.1 live defect, found while verifying this task's OWN
    # copy-tab fallback did not regress Frame detection: a multi-tab
    # document's Framework tab is sometimes ALSO left unnamed ("Tab 1"
    # alongside a named "Content"/"Tab 2"/"English Content" — 6 live
    # documents measured, including several EFFYS location pages).
    # `_FRAME_TAB_NAMES` never matches "Tab 1", and the single-tab
    # fallback above only fires when there is exactly one tab, so
    # `frame_tab` stayed `None` even though a perfectly good Framework
    # table sits right there — silently disabling this task's own lane K
    # (no `frame_item.url` means `run_framework` never fetches the site
    # for these pages at all). Every measured live Framework has >=1
    # recognized field; no measured live Copy tab has any (task-90
    # §1.4a), so "first tab with a recognized field" is a safe,
    # name-independent signal, tried only once the name-based paths above
    # have both failed.
    frame_matched_by_content = False
    if frame_tab is None:
        frame_tab = next((t for t in tabs if _collect_table_fields(t)), None)
        frame_matched_by_content = frame_tab is not None
        if frame_matched_by_content and id(frame_tab) in seen_ids:
            # This same tab was provisionally added to `all_copy_tabs` by
            # the loop above only if it had zero fields — which cannot be
            # true here (we just required `_collect_table_fields(t)` to be
            # non-empty) — kept as a defensive invariant, not a live path.
            all_copy_tabs = [t for t in all_copy_tabs if id(t) != id(frame_tab)]

    frame_doc = None
    if frame_tab is not None:
        item = read_framework_tab(frame_tab, source=source)
        frame_doc = ContentDoc(source=source, doc_type="framework_gdoc", items=(item,))

    copy_doc = None
    copy_sections: tuple[ContentSection, ...] = ()
    if copy_tab is not None:
        copy_item = read_copy_tab(copy_tab, source=source)
        if copy_item is not None:
            copy_doc = ContentDoc(source=source, doc_type="copy_gdoc", items=(copy_item,))
            copy_sections = read_copy_sections(copy_tab)

    return ResolvedTabs(
        frame_doc=frame_doc, copy_doc=copy_doc,
        frame_tab_title=frame_tab.title if frame_tab is not None else None,
        copy_tab_title=copy_tab.title if copy_tab is not None else None,
        unnamed_single_tab=unnamed_single,
        copy_sections=copy_sections,
        all_copy_tabs=tuple(all_copy_tabs),
        frame_matched_by_content=frame_matched_by_content,
    )
