"""Standalone page/blog-фреймворк (DOCX, без сусіднього framework_data.json) ->
ContentDoc. `framework-qa` в AISeoTools цей файл не бере — вона читає лише
JSON, якого в ручного DOCX-брифу немає.

Формат — фіксований шаблон брифу для копірайтера: послідовність підписаних
секцій ("Meta Page Title:", "H1", "Target Keywords", "Structure / Content:", …),
кожна — заголовок-абзац і далі абзаци-значення до наступного відомого заголовка.
Розбір іде через `zipfile` + `xml.etree.ElementTree` по `word/document.xml`
(stdlib, без `python-docx`) — розмітка плоска: `w:p` читається в document order
незалежно від вкладеності в таблицю, що відтворює порядок читання людиною.

`body` свідомо лишається `None`: вміст `Structure / Content:` — це інструкція
копірайтеру («H2: What Is a Schedule of Condition? (direct definition, …)»),
не готова проза сторінки. Прогнати readability/answer-first/section-length по
брифу означає перевіряти інструкцію так, ніби це вже написаний текст, — це
інший тип документа (готова копія від копірайтера), поза обсягом першого
інкременту task-53.
"""

from __future__ import annotations

import io
import re
import zipfile
from xml.etree import ElementTree as ET

from app.contentqa.model import ContentDoc, ContentItem

_NS = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
_HEADING_LINE_RE = re.compile(r"^\s*(H[123])\s*:\s*(.+?)\s*$", re.IGNORECASE)

# Точний (без урахування регістру) список підписів секцій цього шаблону, у
# порядку появи в живому файлі GOREPORT_..._SCHEDULE_OF_CONDITION_..._08182026.docx.
_LABELS = (
    "page:", "url", "top performingcompetitor pages", "top performing pages",
    "competitor pages", "seo details:", "search intent", "serp notes",
    "target keywords", "target words count", "lsi / nlp words",
    "meta page title:", "meta description:", "h1", "structure / content:",
    "external link + anchor text", "internal link + anchor text",
    "seo & content guidelines",
)
_NO_VALUE = {"no", "none", "n/a", "-"}


def _norm_label(text: str) -> str:
    t = re.sub(r"\s+", " ", text or "").strip().lower()
    t = re.sub(r"\(.*?\)", "", t).strip()  # "SERP notes (what top results...)" -> "serp notes"
    return t


def _paragraphs(document_xml: bytes) -> list[str]:
    root = ET.fromstring(document_xml)
    out: list[str] = []
    for p in root.iter(f"{{{_NS['w']}}}p"):
        text = "".join(t.text or "" for t in p.iter(f"{{{_NS['w']}}}t"))
        out.append(text.strip())
    return out


def _sections(paragraphs: list[str]) -> dict[str, list[str]]:
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for para in paragraphs:
        label = _norm_label(para)
        if label in _LABELS:
            current = label
            sections.setdefault(current, [])
            continue
        if current is not None and para:
            sections[current].append(para)
    return sections


def _first(sections: dict[str, list[str]], *labels: str) -> str | None:
    for label in labels:
        if label in sections:
            lines = sections[label]
            return lines[0] if lines else ""
    return None


def _link_lines(sections: dict[str, list[str]], label: str) -> tuple[str, ...]:
    lines = sections.get(label, [])
    return tuple(line for line in lines if line.strip().lower() not in _NO_VALUE)


def _keyword_lines(sections: dict[str, list[str]]) -> tuple[str, ...]:
    lines = sections.get("target keywords", [])
    return tuple(line for line in lines if line and not line.startswith("("))


def _parse_headings(sections: dict[str, list[str]]) -> tuple[tuple[str, str], ...]:
    out: list[tuple[str, str]] = []
    for line in sections.get("structure / content:", []):
        m = _HEADING_LINE_RE.match(line)
        if m:
            out.append((m.group(1).upper(), m.group(2)))
    return tuple(out)


def read_framework_docx(content: bytes, *, source: str) -> ContentDoc:
    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        document_xml = zf.read("word/document.xml")

    paragraphs = _paragraphs(document_xml)
    sections = _sections(paragraphs)

    item = ContentItem(
        ref=source,
        page_name=_first(sections, "page:") or None,
        url=_first(sections, "url"),
        intent=_first(sections, "search intent"),
        title=_first(sections, "meta page title:"),
        description=_first(sections, "meta description:"),
        h1=_first(sections, "h1"),
        headings=_parse_headings(sections),
        keywords=_keyword_lines(sections),
        internal_links=_link_lines(sections, "internal link + anchor text"),
        external_links=_link_lines(sections, "external link + anchor text"),
        body=None,
        raw={k: "\n".join(v) for k, v in sections.items()},
    )
    return ContentDoc(source=source, doc_type="framework_docx", items=(item,))
