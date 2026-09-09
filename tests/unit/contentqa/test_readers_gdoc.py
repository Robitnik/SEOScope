"""readers/gdoc.py — Framework (table-based) + Content (HTML-tagged headings)
tabs -> ContentItem (task-90 §1.4a/§2.3/§4.3).

Fixtures mirror the exact live shapes measured on real documents (SHB, SDS,
SWM) — table rows for Framework fields, literal `<h1>`/`<h2>`/`<h3>` tags
inside NORMAL_TEXT paragraphs for Content headings.
"""

from __future__ import annotations

from app.contentqa.readers.gdoc import (
    read_copy_sections,
    read_copy_tab,
    read_framework_document,
    read_framework_tab,
)
from app.integrations.google_api.docs_client import DocTab


def _para(text: str, style: str | None = None) -> dict[str, object]:
    style_block: dict[str, object] = {"namedStyleType": style} if style else {}
    return {
        "paragraph": {"paragraphStyle": style_block, "elements": [{"textRun": {"content": text}}]}
    }


def _table(*rows: tuple[list[str], list[str]]) -> dict[str, object]:
    """`rows`: `(label_paragraphs, value_paragraphs)` pairs -> a two-column
    table structural element, matching the live "label | value" shape."""
    table_rows = []
    for label_paras, value_paras in rows:
        table_rows.append({
            "tableCells": [
                {"content": [_para(t) for t in label_paras]},
                {"content": [_para(t) for t in value_paras]},
            ]
        })
    return {"table": {"tableRows": table_rows}}


def _tab(
    title: str, content: list[dict[str, object]], path: tuple[str, ...] | None = None
) -> DocTab:
    return DocTab(tab_id="t.0", title=title, path=path or (title,), body_content=tuple(content))


# ── Framework tab: table-based field extraction ──────────────────────────────


def test_framework_extracts_all_fields_from_table_rows() -> None:
    """Mirrors the live SHB_FRAMEWORK_HOME_PAGE shape."""
    tab = _tab("Framework", [
        _para("SHB - Home Page"),
        _para("Page:", style="HEADING_2"),
        _table((["URL"], ["https://www.smashhouseburgers.com/"])),
        _para("SEO Details:", style="HEADING_2"),
        _table(
            (["Search Intent"], ["Brand + commercial category discovery."]),
            (
                ["Target Keywords", "(first keyword is the primary)"],
                ["kosher smash burger", "smash burgers"],
            ),
            (["Meta Page Title:"], ["Kosher Smash Burgers | Chicken, Fries & More"]),
            (["Meta Description:"], ["Smash House serves Glatt Kosher smash burgers."]),
            (["H1"], ["The First Glatt Kosher Smashed Burger Restaurant in America"]),
            (["Structure / Content:"], [
                "H1: The First Glatt Kosher Smashed Burger Restaurant in America",
                "[Intro, 50-70 words, answer-first.]",
                "H2: Our Most Popular Smash Burgers, Chicken & Fries",
                "H3: Dirty Fries",
            ]),
            (["External link + Anchor Text"], ["No"]),
            (["Internal Link + Anchor Text"], [
                "kosher smash burgers menu → /menu/",
                "burger catering → /catering/",
            ]),
        ),
    ])
    item = read_framework_tab(tab, source="SHB_FRAMEWORK_HOME_PAGE")
    assert item.url == "https://www.smashhouseburgers.com/"
    assert item.intent == "Brand + commercial category discovery."
    assert item.title == "Kosher Smash Burgers | Chicken, Fries & More"
    assert item.description == "Smash House serves Glatt Kosher smash burgers."
    assert item.h1 == "The First Glatt Kosher Smashed Burger Restaurant in America"
    assert item.keywords == ("kosher smash burger", "smash burgers")
    assert item.headings == (
        ("H1", "The First Glatt Kosher Smashed Burger Restaurant in America"),
        ("H2", "Our Most Popular Smash Burgers, Chicken & Fries"),
        ("H3", "Dirty Fries"),
    )
    assert item.internal_links == (
        "kosher smash burgers menu → /menu/", "burger catering → /catering/",
    )
    assert item.external_links == ()  # "No" filtered as _NO_VALUE
    assert item.body is None  # always None for Framework — it's a brief, not prose


def test_framework_label_second_paragraph_is_a_discarded_hint() -> None:
    """A label cell with a second paragraph in parentheses (e.g. "Search
    Intent" + "(guide, list, comparison...)") must match on the first
    paragraph alone."""
    label_paras = ["Search Intent", "(guide, list, comparison, local page, etc.)"]
    tab = _tab("Framework", [_table((label_paras, ["Local research intent."]))])
    item = read_framework_tab(tab, source="doc")
    assert item.intent == "Local research intent."


def test_framework_soft_line_break_splits_value_into_multiple_lines() -> None:
    """`\\x0b` inside ONE value paragraph is a list separator, not a single
    blob — live example: three anchors crammed into one table cell paragraph."""
    anchors = "Dental Implants\x0bDental Crowns\x0bContact Us"
    tab = _tab("Framework", [_table((["Internal Link + Anchor Text"], [anchors]))])
    item = read_framework_tab(tab, source="doc")
    assert item.internal_links == ("Dental Implants", "Dental Crowns", "Contact Us")


def test_framework_alternate_structure_label_is_recognized() -> None:
    """The short brief template spells the field "Structure, content" (comma,
    no colon) instead of "Structure / Content:" — both must map to the same
    field (task-90 §1.4a, two different live templates)."""
    tab = _tab("Framework", [
        _table((["Structure, content"], ["H1: Some Title", "H2: A Section"])),
    ])
    item = read_framework_tab(tab, source="doc")
    assert item.headings == (("H1", "Some Title"), ("H2", "A Section"))


def test_framework_unrecognized_label_is_ignored_not_crashed_on() -> None:
    tab = _tab("Framework", [
        _table((["Top Competitor Examples"], ["https://example.com/rival"])),
        _table((["H1"], ["Real Title"])),
    ])
    item = read_framework_tab(tab, source="doc")
    assert item.h1 == "Real Title"
    assert "Top Competitor Examples" not in item.raw


def test_framework_missing_field_is_none_not_empty_string() -> None:
    """PLAN §12: a field with no table row at all is `None`; only a row that
    exists with an empty value becomes `""`."""
    tab = _tab("Framework", [_table((["H1"], [""]))])
    item = read_framework_tab(tab, source="doc")
    assert item.h1 == ""  # present, empty
    assert item.title is None  # no row at all


# ── Content tab: literal HTML heading tags, not native styles ────────────────


def test_content_html_heading_tags_are_parsed() -> None:
    """Live: 100% of measured Content prose tabs use literal `<h1>/<h2>/<h3>`
    tags in NORMAL_TEXT paragraphs, 0% use native HEADING_* styles."""
    tab = _tab("Content", [
        _para("<h1>Turkish Food in Brooklyn: A Neighborhood Guide</h1>"),
        _para("If you're looking for Turkish food in Brooklyn, expect grilled meat."),
        _para("<h2>Turkish Food vs. Mediterranean Food</h2>"),
        _para("Turkish cuisine is part of the wider Mediterranean food story."),
        _para("<h3>Turkish Cuisine Has Its Own Traditions</h3>"),
        _para("The Turkish grill is a big part of the picture."),
    ])
    item = read_copy_tab(tab, source="doc")
    assert item is not None
    assert item.h1 == "Turkish Food in Brooklyn: A Neighborhood Guide"
    assert item.headings == (
        ("H1", "Turkish Food in Brooklyn: A Neighborhood Guide"),
        ("H2", "Turkish Food vs. Mediterranean Food"),
        ("H3", "Turkish Cuisine Has Its Own Traditions"),
    )
    assert item.body is not None
    assert "<h1>" not in item.body and "<h2>" not in item.body  # headings excluded from body
    assert "If you're looking for Turkish food in Brooklyn" in item.body


def test_content_native_heading_style_is_a_fallback_path() -> None:
    """No live document was found using native styles for body copy, but the
    fallback stays supported and tested in case one ever does."""
    tab = _tab("Content", [
        _para("A Native-Style Heading", style="HEADING_2"),
        _para("Plain body text under it."),
    ])
    item = read_copy_tab(tab, source="doc")
    assert item is not None
    assert item.headings == (("H2", "A Native-Style Heading"),)


def test_content_empty_tab_returns_none() -> None:
    """Live: 2 of 18 documents with both tabs have a `Content` tab with a
    single 1-character paragraph — must not look like a checked, clean copy."""
    tab = _tab("Content", [_para("\n")])
    assert read_copy_tab(tab, source="doc") is None


def test_content_whitespace_only_tab_returns_none() -> None:
    tab = _tab("Content", [_para(""), _para("   \n")])
    assert read_copy_tab(tab, source="doc") is None


def test_read_copy_sections_groups_prose_by_heading() -> None:
    tab = _tab("Content", [
        _para("<h1>Title</h1>"),
        _para("Intro paragraph."),
        _para("<h2>Section A</h2>"),
        _para("Body of section A."),
    ])
    sections = read_copy_sections(tab)
    assert len(sections) == 2
    assert sections[0].level == "H1"
    assert sections[0].heading == "Title"
    assert sections[0].text == "Intro paragraph."
    assert sections[1].level == "H2"
    assert sections[1].text == "Body of section A."


def test_read_copy_sections_empty_for_no_headings() -> None:
    tab = _tab("Content", [_para("Just prose, no heading at all.")])
    assert read_copy_sections(tab) == ()


def test_content_first_h1_wins_if_multiple() -> None:
    tab = _tab("Content", [_para("<h1>First</h1>"), _para("<h1>Second</h1>")])
    item = read_copy_tab(tab, source="doc")
    assert item is not None
    assert item.h1 == "First"


# ── read_framework_document: tab resolution (task-90 §2.3) ───────────────────


def test_document_with_framework_and_content_tabs() -> None:
    tabs = (
        _tab("Framework", [_table((["H1"], ["Title A"]))]),
        _tab("Content", [_para("<h1>Title A</h1>"), _para("Body prose.")]),
    )
    resolved = read_framework_document(tabs, source="doc")
    assert resolved.frame_doc is not None
    assert resolved.copy_doc is not None
    assert resolved.frame_tab_title == "Framework"
    assert resolved.copy_tab_title == "Content"
    assert resolved.unnamed_single_tab is False
    assert len(resolved.copy_sections) == 1
    assert resolved.copy_sections[0].heading == "Title A"


def test_document_with_only_framework_tab_has_no_copy() -> None:
    """Live: 11 of 40 documents have only `Framework`, no `Content` at all."""
    tabs = (_tab("Framework", [_table((["H1"], ["Title A"]))]),)
    resolved = read_framework_document(tabs, source="doc")
    assert resolved.frame_doc is not None
    assert resolved.copy_doc is None
    assert resolved.copy_tab_title is None


def test_document_with_empty_content_tab_reports_title_but_no_doc() -> None:
    """The tab exists (so `copy_tab_title` is set) but has no usable prose —
    the caller must be able to distinguish this from "no Content tab at
    all" for the `not_checked` reason text."""
    tabs = (
        _tab("Framework", [_table((["H1"], ["Title A"]))]),
        _tab("Content", [_para("\n")]),
    )
    resolved = read_framework_document(tabs, source="doc")
    assert resolved.copy_doc is None
    assert resolved.copy_tab_title == "Content"


def test_single_unnamed_tab_is_read_as_framework() -> None:
    """Live: 11 of 40 documents have only the default 'Tab 1', never
    renamed."""
    tabs = (_tab("Tab 1", [_table((["H1"], ["Title A"]))]),)
    resolved = read_framework_document(tabs, source="doc")
    assert resolved.frame_doc is not None
    assert resolved.unnamed_single_tab is True


def test_tab_name_synonyms_are_recognized() -> None:
    tabs = (
        _tab("Page Framework", [_table((["H1"], ["X"]))]),
        _tab("Copy", [_para("<h1>X</h1>"), _para("prose")]),
    )
    resolved = read_framework_document(tabs, source="doc")
    assert resolved.frame_doc is not None
    assert resolved.copy_doc is not None


def test_no_tabs_at_all_resolves_to_nothing() -> None:
    resolved = read_framework_document((), source="doc")
    assert resolved.frame_doc is None
    assert resolved.copy_doc is None
    assert resolved.unnamed_single_tab is False


def test_multiple_unrecognized_tabs_do_not_fall_back() -> None:
    """The single-unnamed-tab fallback only applies when there is EXACTLY
    one tab — two unrecognized tabs must not silently pick one."""
    tabs = (_tab("Draft 1", []), _tab("Draft 2", []))
    resolved = read_framework_document(tabs, source="doc")
    assert resolved.frame_doc is None
    assert resolved.copy_doc is None
    assert resolved.unnamed_single_tab is False


def test_child_tab_ref_includes_path() -> None:
    tab = _tab("Content", [_para("<h1>X</h1>"), _para("prose")], path=("Page A", "Content"))
    item = read_copy_tab(tab, source="MyDoc")
    assert item is not None
    assert item.ref == "MyDoc › Page A › Content"
