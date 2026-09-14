"""readers/gdoc.py — Framework (table-based) + Content (HTML-tagged headings)
tabs -> ContentItem (task-90 §1.4a/§2.3/§4.3).

Fixtures mirror the exact live shapes measured on real documents (SHB, SDS,
SWM) — table rows for Framework fields, literal `<h1>`/`<h2>`/`<h3>` tags
inside NORMAL_TEXT paragraphs for Content headings.
"""

from __future__ import annotations

from app.contentqa.readers.gdoc import (
    copy_tab_blocks,
    copy_tab_links,
    read_copy_sections,
    read_copy_tab,
    read_framework_document,
    read_framework_tab,
)
from app.integrations.google_api.docs_client import DocTab


def _para(text: str, style: str | None = None, *, bullet: bool = False) -> dict[str, object]:
    style_block: dict[str, object] = {"namedStyleType": style} if style else {}
    paragraph: dict[str, object] = {
        "paragraphStyle": style_block, "elements": [{"textRun": {"content": text}}]
    }
    if bullet:
        # Live shape: a non-empty `Bullet` object — `bool({})` is `False`,
        # so a fixture using `{}` would silently fail to exercise the
        # bullet path (task-92 §1.3).
        paragraph["bullet"] = {"listId": "kix.list", "nestingLevel": 0}
    return {"paragraph": paragraph}


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


def test_framework_dash_placeholder_url_reads_as_present_but_empty() -> None:
    """task-97 §2.5: a `Page: -` row (placeholder, no real URL yet) must
    NOT read as a real URL — the same class of bug fix `0674487` closed on
    the strategy side (`readers/tabular.py:_row_to_item`): a literal `-`
    would otherwise build the key `url:-` in `crosscheck.py:_item_key()`
    and remain a candidate for a false match. `""`, not `None` — the row
    IS present, only its value is a placeholder (PLAN §12)."""
    tab = _tab("Framework", [_table((["URL"], ["-"]))])
    item = read_framework_tab(tab, source="doc")
    assert item.url == ""


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


# ── task-92 §1.1: broadened Copy-tab recognition ─────────────────────────────


def test_copy_tab_recognized_by_word_boundary_substring() -> None:
    """Live: 49/96 real copy tabs are named things `_COPY_TAB_NAMES`'
    exact-match set never covers — "English Content"/"Swedish Content"
    (LAXTON) among them."""
    tabs = (
        _tab("Framework", [_table((["H1"], ["Title A"]))]),
        _tab("English Content", [_para("<h1>Title A</h1>"), _para("Body prose.")]),
    )
    resolved = read_framework_document(tabs, source="doc")
    assert resolved.copy_doc is not None
    assert resolved.copy_tab_title == "English Content"
    assert [t.title for t in resolved.all_copy_tabs] == ["English Content"]


def test_copy_tab_recognized_by_underscored_client_name() -> None:
    """Live: `SAPHNY_COPY_WEBSITE_CLIENT_HOMEPAGE_031224` — underscores must
    fold to spaces before the word-boundary check, or `\\bcopy\\b` never
    fires inside an all-underscore name."""
    tabs = (
        _tab("Framework", [_table((["H1"], ["Title A"]))]),
        _tab(
            "SAPHNY_COPY_WEBSITE_CLIENT_HOMEPAGE_031224",
            [_para("<h1>Title A</h1>"), _para("Body prose.")],
        ),
    )
    resolved = read_framework_document(tabs, source="doc")
    assert resolved.copy_doc is not None


def test_copy_tab_recognized_structurally_when_bare_and_unnamed() -> None:
    """Live: EFFYS FAQ — the copy tab is literally "Tab 2", no name at
    all. Zero recognized table fields + real content is copy-like
    regardless of name."""
    tabs = (
        _tab("Framework", [_table((["H1"], ["Title A"]))]),
        _tab("Tab 2", [_para("<h1>FAQ</h1>"), _para("Answer body long enough to count.")]),
    )
    resolved = read_framework_document(tabs, source="doc")
    assert resolved.copy_doc is not None
    assert resolved.copy_tab_title == "Tab 2"


def test_trivial_unnamed_tab_is_not_a_phantom_copy_tab() -> None:
    """A stray short note must not become a fake Copy tab just because it
    has zero table fields (task-92 §1.1 `_MIN_COPY_CANDIDATE_CHARS`)."""
    tabs = (
        _tab("Framework", [_table((["H1"], ["Title A"]))]),
        _tab("Notes", [_para("TBD")]),
    )
    resolved = read_framework_document(tabs, source="doc")
    assert resolved.copy_doc is None
    assert resolved.all_copy_tabs == ()


def test_lone_website_copy_tab_is_copy_not_framework() -> None:
    """Live defect (task-92 §1.1): 19 documents are a SINGLE tab of
    finished copy whose title IS the document name ("Brandons Donuts
    Website Copy"). The pre-task-92 single-tab fallback silently read
    every lone unnamed tab as Framework — must now check for recognized
    brief fields first."""
    tabs = (_tab("Brandons Donuts Website Copy", [_para("<h1>Donuts</h1>"), _para("Body.")]),)
    resolved = read_framework_document(tabs, source="doc")
    assert resolved.frame_doc is None
    assert resolved.copy_doc is not None
    assert resolved.unnamed_single_tab is False


def test_lone_tab_with_fields_is_still_framework() -> None:
    """The single-tab fallback must still read a real brief as Framework
    when it DOES carry recognized fields — the fix above must not flip
    every lone tab to Copy."""
    tabs = (_tab("Tab 1", [_table((["H1"], ["Title A"]))]),)
    resolved = read_framework_document(tabs, source="doc")
    assert resolved.frame_doc is not None
    assert resolved.unnamed_single_tab is True


def test_frame_tab_matched_by_content_when_unnamed_among_others() -> None:
    """Live defect (task-92 §1.1, 6 documents including several EFFYS
    location pages): a multi-tab document's Framework tab is sometimes
    ALSO left unnamed ("Tab 1") alongside a named "Content"/"Tab 2" — the
    exact-name check and the single-tab fallback both miss it, silently
    disabling this task's own lane K (no URL to fetch)."""
    tabs = (
        _tab("Tab 1", [_table((["H1"], ["Title A"]), (["URL"], ["https://example.com/p"]))]),
        _tab("Content", [_para("<h1>Title A</h1>"), _para("Body prose.")]),
    )
    resolved = read_framework_document(tabs, source="doc")
    assert resolved.frame_doc is not None
    assert resolved.frame_tab_title == "Tab 1"
    assert resolved.frame_matched_by_content is True
    assert resolved.copy_doc is not None


def test_all_copy_tabs_lists_every_language_variant() -> None:
    """LAXTON shape: Framework + English Content + Swedish Content on one
    document — `all_copy_tabs` must carry BOTH, not just the first found
    by name (task-92 §1.8/§2.5, role selection needs every candidate)."""
    tabs = (
        _tab("Framework", [_table((["H1"], ["Spirit Box"]))]),
        _tab("English Content", [_para("<h1>Spirit Box</h1>"), _para("English body.")]),
        _tab("Swedish Content", [_para("<h1>Spirit Box</h1>"), _para("Svensk text.")]),
    )
    resolved = read_framework_document(tabs, source="doc")
    assert [t.title for t in resolved.all_copy_tabs] == ["English Content", "Swedish Content"]


def test_all_copy_tabs_excludes_the_frame_tab_itself() -> None:
    tabs = (_tab("Framework", [_table((["H1"], ["X"]))]),)
    resolved = read_framework_document(tabs, source="doc")
    assert resolved.all_copy_tabs == ()


# ── task-92 §1.3/§2.2: copy_tab_blocks — block-level stream for copydiff ────


def test_copy_tab_blocks_splits_soft_line_breaks_into_separate_items() -> None:
    """Live defect: `\\x0b`-joined bullet lines inside one paragraph must
    become separate blocks, not one run-on sentence."""
    text = "Real-time EVP scanning\x0bBuilt-in speaker\x0bRed display backlight"
    tab = _tab("Content", [_para(text, bullet=True)])
    blocks = copy_tab_blocks(tab)
    assert [b.text for b in blocks] == [
        "Real-time EVP scanning", "Built-in speaker", "Red display backlight",
    ]
    assert all(b.kind == "item" for b in blocks)


def test_copy_tab_blocks_bullet_paragraph_is_an_item_not_split_by_style() -> None:
    tab = _tab("Content", [_para("Just one bullet line.", bullet=True)])
    (block,) = copy_tab_blocks(tab)
    assert block.kind == "item"


def test_copy_tab_blocks_recognizes_html_heading_without_closing_tag() -> None:
    """Live (EFFYS COLUMBUS_AVE): `<h2>Effy's Café & Patisserie: Columbus
    Ave` with no `</h2>` at all — Docs lets a copywriter forget the close,
    the paragraph still reads as a heading."""
    tab = _tab("Content", [_para("<h2>Effy's Café & Patisserie: Columbus Ave")])
    (block,) = copy_tab_blocks(tab)
    assert block.kind == "heading"
    assert block.level == "H2"
    assert block.text == "Effy's Café & Patisserie: Columbus Ave"


def test_copy_tab_blocks_recognizes_bare_heading_label() -> None:
    tab = _tab("Content", [_para("H1: Effy's Café Columbus Ave")])
    (block,) = copy_tab_blocks(tab)
    assert block.kind == "heading"
    assert block.level == "H1"
    assert block.text == "Effy's Café Columbus Ave"


def test_copy_tab_blocks_strips_editor_marker_and_flags_it() -> None:
    tab = _tab("Content", [_para("Position and content unchanged: [KEEP]")])
    (block,) = copy_tab_blocks(tab)
    assert block.kind == "para"
    assert block.text == "Position and content unchanged:"
    assert block.has_marker is True


def test_copy_tab_blocks_extended_marker_form_is_recognized() -> None:
    """Live (3XE): `[KEEP heading and form]`, not just the bare keyword."""
    tab = _tab("Content", [_para("Ladies Free Friday [KEEP heading and form]")])
    (block,) = copy_tab_blocks(tab)
    assert block.has_marker is True
    assert block.text == "Ladies Free Friday"


def test_copy_tab_blocks_bracketed_aside_is_a_directive() -> None:
    tab = _tab("Content", [
        _para("[IMAGE SECTION: Full-width hero background image]"),
        _para("Real prose sentence here."),
    ])
    blocks = copy_tab_blocks(tab)
    assert blocks[0].kind == "directive"
    assert blocks[1].kind == "para"


def test_copy_tab_blocks_labelled_directive_line_is_excluded() -> None:
    tab = _tab("Content", [_para("Breadcrumbs: Home / Locations / Effy's Café UWS")])
    (block,) = copy_tab_blocks(tab)
    assert block.kind == "directive"


def test_copy_tab_blocks_rule_line_is_a_directive() -> None:
    tab = _tab("Content", [_para("_______________________________")])
    (block,) = copy_tab_blocks(tab)
    assert block.kind == "directive"


def test_copy_tab_blocks_plain_prose_is_a_para() -> None:
    tab = _tab("Content", [_para("Effy's offers outdoor seating, weather permitting.")])
    (block,) = copy_tab_blocks(tab)
    assert block.kind == "para"
    assert block.has_marker is False


def test_copy_tab_blocks_rule_line_glued_to_a_directive_is_a_directive() -> None:
    """Live (EFFYS COLUMBUS_AVE): `"____...___[IMAGE SECTION: hero]"` as ONE
    paragraph with no separator — neither the pure rule-line nor the
    bracketed-aside pattern matched on its own, and the placeholder showed
    up in the errors-only Copy sheet as a fake "missing" sentence."""
    tab = _tab("Content", [_para("_" * 58 + "[IMAGE SECTION: Full-width hero background image]")])
    (block,) = copy_tab_blocks(tab)
    assert block.kind == "directive"
    assert block.text == "[IMAGE SECTION: Full-width hero background image]"


# ── task-95 §1.4/§2.3: copy_tab_links — hyperlinks a copy tab carries ───────


def _run(content: str, url: str | None = None) -> dict[str, object]:
    text_run: dict[str, object] = {"content": content}
    if url:
        text_run["textStyle"] = {"link": {"url": url}}
    return {"textRun": text_run}


def _link_para(*runs: dict[str, object]) -> dict[str, object]:
    return {"paragraph": {"elements": list(runs)}}


def test_copy_tab_links_extracts_a_plain_hyperlink() -> None:
    tab = _tab("Content", [_link_para(_run("our menu", "https://example.com/menu/"))])
    (link,) = copy_tab_links(tab)
    assert link.url == "https://example.com/menu/"
    assert link.anchor == "our menu"
    assert link.in_marker is False


def test_copy_tab_links_merges_adjacent_runs_sharing_the_same_url() -> None:
    """Docs often splits one visible anchor into several `textRun`s (an
    empty run before the visible text, or formatting inside the anchor) —
    live-measured on LAXTON/EFFYS. Merged into ONE `CopyLink`, not two."""
    tab = _tab("Content", [_link_para(
        _run("", "https://example.com/prs/"),
        _run("Paranormal REM Static", "https://example.com/prs/"),
    )])
    (link,) = copy_tab_links(tab)
    assert link.anchor == "Paranormal REM Static"
    assert link.url == "https://example.com/prs/"


def test_copy_tab_links_keeps_separate_runs_with_different_urls_apart() -> None:
    tab = _tab("Content", [_link_para(
        _run("our menu", "https://example.com/menu/"),
        _run(" and ", None),
        _run("our story", "https://example.com/about/"),
    )])
    links = copy_tab_links(tab)
    assert [link.url for link in links] == [
        "https://example.com/menu/", "https://example.com/about/",
    ]


def test_copy_tab_links_ignores_plain_text_without_a_link() -> None:
    tab = _tab("Content", [_link_para(_run("just some prose, no link here"))])
    assert copy_tab_links(tab) == ()


def test_copy_tab_links_ignores_internal_anchor_without_a_url() -> None:
    # `textStyle.link` can point at a `headingId`/`bookmarkId` inside the
    # SAME document (an internal cross-reference), never a `url` — not an
    # external/page link the site-audit chain cares about.
    tab = _tab("Content", [{"paragraph": {"elements": [
        {"textRun": {"content": "see above", "textStyle": {"link": {"headingId": "h.abc"}}}},
    ]}}])
    assert copy_tab_links(tab) == ()


def test_copy_tab_links_flags_instruction_paragraphs_as_in_marker() -> None:
    """task-95 §1.4/§2.3, live EFFYS_LOCATIONS_HUB: `[MAP: ...]`/
    `[Order Pickup or Delivery →]` are verstalnyk instructions, not copy
    the operator should expect published — `in_marker=True`, still
    returned (shown, not silently dropped) so the caller can choose to
    exclude them from the verdict while still surfacing them."""
    tab = _tab("Content", [_link_para(
        _run("[Order Pickup or Delivery →]", "https://www.getsauce.com/order/x"),
    )])
    (link,) = copy_tab_links(tab)
    assert link.in_marker is True


def test_copy_tab_links_never_reads_the_framework_tab_boilerplate() -> None:
    """The Framework tab's own tool-links (Hemingway/Title Case
    Converter/Grammarly) are present in EVERY live document (task-95
    §1.4) — `copy_tab_links` only ever gets called on a COPY tab by its
    caller, but the function itself doesn't special-case tab identity;
    this asserts it simply extracts whatever hyperlinks the given tab
    carries, boilerplate or not — the exclusion is the caller's job."""
    tab = _tab("Framework", [_link_para(_run("Hemingway Score", "https://hemingwayapp.com/"))])
    (link,) = copy_tab_links(tab)
    assert link.url == "https://hemingwayapp.com/"
