from __future__ import annotations

import io
import zipfile
from xml.sax.saxutils import escape

from app.contentqa.readers.docx import read_framework_docx

_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _p(text: str) -> str:
    return f'<w:p xmlns:w="{_NS}"><w:r><w:t>{escape(text)}</w:t></w:r></w:p>'


def _build_docx(*paragraphs: str) -> bytes:
    body = "".join(_p(p) for p in paragraphs)
    document_xml = (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w:document xmlns:w="{_NS}"><w:body>{body}</w:body></w:document>'
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("word/document.xml", document_xml)
    return buf.getvalue()


_SAMPLE = _build_docx(
    "Page:",
    "URL",
    "https://example.com/schedule-of-condition-software/",
    "SEO Details:",
    "Search Intent",
    "(guide, list, comparison, local page, etc.)",
    "Commercial software evaluation page",
    "Target Keywords",
    "(first keyword is the primary)",
    "schedule of condition software",
    "what is a schedule of condition",
    "Target Words Count",
    "scope-based estimate: ~850-1150",
    "Meta Page Title:",
    "Schedule of Condition (SOC) Software | GoReport",
    "Meta Description:",
    "Digital Schedule of Condition surveys with photo capture and branded outputs. Book a demo.",
    "H1",
    "Schedule of Condition Software for Commercial Property Surveyors",
    "Structure / Content:",
    "H1: Schedule of Condition Software for Commercial Property Surveyors",
    "Intro: names the page.",
    "H2: What Is a Schedule of Condition?",
    "H2: Frequently Asked Questions",
    "H3: What Does GoReport Capture?",
    "External link + Anchor text",
    "No",
    "Internal Link + Anchor Text",
    "party wall survey (/party-wall-survey/)",
    "dilapidations survey (/dilapidations-survey/)",
    "SEO & Content Guidelines",
    "Write for the reader first.",
)


def test_meta_fields_extracted() -> None:
    doc = read_framework_docx(_SAMPLE, source="sample.docx")
    item = doc.items[0]
    assert item.title == "Schedule of Condition (SOC) Software | GoReport"
    assert item.description.startswith("Digital Schedule of Condition surveys")
    assert item.h1 == "Schedule of Condition Software for Commercial Property Surveyors"
    assert item.url == "https://example.com/schedule-of-condition-software/"


def test_headings_parsed_from_structure_block() -> None:
    doc = read_framework_docx(_SAMPLE, source="sample.docx")
    item = doc.items[0]
    expected_h1 = "Schedule of Condition Software for Commercial Property Surveyors"
    assert ("H1", expected_h1) in item.headings
    assert ("H2", "What Is a Schedule of Condition?") in item.headings
    assert ("H3", "What Does GoReport Capture?") in item.headings


def test_keywords_exclude_parenthetical_instruction_line() -> None:
    doc = read_framework_docx(_SAMPLE, source="sample.docx")
    item = doc.items[0]
    assert item.keywords == ("schedule of condition software", "what is a schedule of condition")


def test_external_links_empty_when_doc_says_no() -> None:
    doc = read_framework_docx(_SAMPLE, source="sample.docx")
    assert doc.items[0].external_links == ()


def test_internal_links_collected() -> None:
    doc = read_framework_docx(_SAMPLE, source="sample.docx")
    item = doc.items[0]
    assert len(item.internal_links) == 2
    assert "party wall survey (/party-wall-survey/)" in item.internal_links


def test_body_is_none_brief_is_not_page_copy() -> None:
    doc = read_framework_docx(_SAMPLE, source="sample.docx")
    assert doc.items[0].body is None
