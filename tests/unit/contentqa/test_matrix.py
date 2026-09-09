"""contentqa/matrix.py — Strategy/Framework/Site comparison matrix (task-91).

Normalizer tests use the exact live-measured strings from task-91 §1.4-1.6
(SHB keyword merging, SDS/SAPHNY parens, 3XE arrows, LAXTON's misleading
arrow, PJC/EFFYS's URL-less prose) — not invented examples.
"""

from __future__ import annotations

from app.contentqa.crosscheck import _item_key
from app.contentqa.matrix import (
    Cell,
    _document_presence,
    _extract_link_target,
    _framework_cells,
    _keyword_verdict,
    _link_verdict,
    _norm_keyword_set,
    _norm_link_set,
    _norm_text,
    _scalar_verdict,
    _site_cells,
    _site_keyword_coverage,
    _strategy_cells,
    build_matrix,
)
from app.contentqa.model import ContentDoc, ContentItem
from app.contentqa.package import FrameworkOutcome, FrameworkSource
from app.contentqa.site import LivePage
from app.monitor.page import PageSignals

# ── _norm_text ──────────────────────────────────────────────────────────


def test_norm_text_folds_case_and_whitespace() -> None:
    # SHB §1.6: "Our Smashed Burgers Menu" (framework) vs "OUR SMASHED
    # BURGERS MENU" (live site) — not a real SEO drift.
    assert _norm_text("Our Smashed Burgers Menu") == _norm_text("OUR SMASHED BURGERS MENU")


def test_norm_text_collapses_internal_whitespace() -> None:
    assert _norm_text("Smash  House |  Menu") == _norm_text("Smash House | Menu")


def test_norm_text_none_is_empty_string() -> None:
    assert _norm_text(None) == ""


# ── _norm_keyword_set (§1.5) ────────────────────────────────────────────


def test_keywords_with_embedded_newlines_split_into_separate_tokens() -> None:
    # Live SHB strategy cell: three keywords glued into one tuple element.
    strategy_kw = (
        "smash house kosher",
        "smash house burgers\nsmash house burger\nsmash house burger kosher",
        "is smash house kosher",
    )
    framework_kw = (
        "smash house kosher",
        "smash house burgers",
        "smash house burger",
        "smash house burger kosher",
        "is smash house kosher",
    )
    assert _norm_keyword_set(strategy_kw) == _norm_keyword_set(framework_kw)


def test_keyword_set_dedupes_and_casefolds() -> None:
    assert _norm_keyword_set(("Kosher Burger", "kosher burger", "  Kosher Burger  ")) == {
        "kosher burger"
    }


def test_empty_keyword_tuple_gives_empty_set() -> None:
    assert _norm_keyword_set(()) == frozenset()


# ── _extract_link_target (§1.4/§1.4a) ───────────────────────────────────


def test_extracts_absolute_url_strategy_format() -> None:
    # Live SDS strategy cell.
    raw = "INT LINK 1 - https://www.shorelinedentalstudio.com/services/invisalign/"
    assert (
        _extract_link_target(raw, base_url=None)
        == "https://www.shorelinedentalstudio.com/services/invisalign/"
    )


def test_extracts_parenthesized_path_sds_saphny_format() -> None:
    raw = (
        'Dental Implants (/services/tooth-replacement/dental-implants/) '
        '- in "Discuss Implant Support"'
    )
    assert (
        _extract_link_target(raw, base_url="https://example.com/x")
        == "https://example.com/services/tooth-replacement/dental-implants/"
    )


def test_extracts_path_after_arrow_3xe_format() -> None:
    raw = "2026 EXXXOTICA locations → /locations/"
    assert (
        _extract_link_target(raw, base_url="https://exxxotica.com/home")
        == "https://exxxotica.com/locations/"
    )


def test_arrow_followed_by_non_path_is_not_extracted_laxton_format() -> None:
    # Live LAXTON cell: the arrow is there, but what follows is a
    # placement description, not a URL — a naive "everything after the
    # arrow" extractor would have produced a garbage "URL".
    raw = 'utrustning för spökjakt → placement: H2 "Utrustningen bakom undersökningarna"'
    assert _extract_link_target(raw, base_url="https://laxton.se/artikel") is None


def test_plain_prose_with_no_url_is_not_extracted_pjc_effys_format() -> None:
    assert _extract_link_target("make a reservation", base_url="https://pjclarkes.com/") is None
    assert _extract_link_target("Effy's UWS location", base_url="https://effys.com/") is None


def test_relative_path_without_base_url_is_returned_as_is() -> None:
    assert _extract_link_target("anchor → /menu/", base_url=None) == "/menu/"


def test_norm_link_set_dedupes_via_inspector_key() -> None:
    values = (
        "Our menu → /menu/",
        "See the menu → /menu",  # same page, no trailing slash
    )
    keys = _norm_link_set(values, base_url="https://example.com/")
    assert len(keys) == 1


# ── _document_presence (§1.2, the None-vs-absent finding) ───────────────


def test_document_presence_true_when_any_row_has_a_value() -> None:
    # Live SDS shape: url column exists, most rows filled, some blank.
    doc = ContentDoc(
        source="s",
        doc_type="strategy_gsheet",
        items=(
            ContentItem(ref="r1", url="https://example.com/a"),
            ContentItem(ref="r2", url=None),  # blank cell, column still exists
        ),
    )
    assert "url" in _document_presence(doc)


def test_document_presence_false_when_column_never_has_a_value() -> None:
    # Live SHB shape: no URL column at all in the sheet.
    doc = ContentDoc(
        source="s",
        doc_type="strategy_gsheet",
        items=(ContentItem(ref="r1", url=None), ContentItem(ref="r2", url=None)),
    )
    assert "url" not in _document_presence(doc)


# ── _strategy_cells: three states ────────────────────────────────────────


def test_strategy_cell_is_absent_when_document_never_has_the_column() -> None:
    doc = ContentDoc(source="s", doc_type="strategy_gsheet", items=(ContentItem(ref="r1"),))
    presence = _document_presence(doc)
    cells = _strategy_cells(doc.items[0], presence)
    assert cells.url.state == "absent"


def test_strategy_cell_is_empty_when_column_exists_but_this_row_is_blank() -> None:
    doc = ContentDoc(
        source="s",
        doc_type="strategy_gsheet",
        items=(
            ContentItem(ref="r1", url="https://example.com/a"),
            ContentItem(ref="r2", url=None),
        ),
    )
    presence = _document_presence(doc)
    cells = _strategy_cells(doc.items[1], presence)
    assert cells.url.state == "empty"


def test_strategy_cell_is_value_when_present() -> None:
    doc = ContentDoc(
        source="s", doc_type="strategy_gsheet", items=(ContentItem(ref="r1", title="Hello"),)
    )
    cells = _strategy_cells(doc.items[0], _document_presence(doc))
    assert cells.title == Cell(state="value", text="Hello")


def test_strategy_cell_is_absent_for_none_item() -> None:
    cells = _strategy_cells(None, frozenset())
    assert cells.url.state == "absent"
    assert cells.keywords.state == "absent"


# ── _framework_cells: exact None/"" from readers/gdoc.py ────────────────


def test_framework_cell_absent_when_field_never_recognized() -> None:
    item = ContentItem(ref="r1", title="T", raw={"title": "T"})
    cells = _framework_cells(item)
    assert cells.description.state == "absent"


def test_framework_cell_empty_when_recognized_but_blank() -> None:
    item = ContentItem(ref="r1", title="", raw={"title": ""})
    cells = _framework_cells(item)
    assert cells.title.state == "empty"


def test_framework_keywords_absent_vs_empty_via_raw() -> None:
    no_keywords_field = ContentItem(ref="r1", raw={"title": "T"})
    assert _framework_cells(no_keywords_field).keywords.state == "absent"

    blank_keywords_field = ContentItem(ref="r1", keywords=(), raw={"title": "T", "keywords": ""})
    assert _framework_cells(blank_keywords_field).keywords.state == "empty"

    filled = ContentItem(ref="r1", keywords=("a", "b"), raw={"keywords": "a\nb"})
    assert _framework_cells(filled).keywords == Cell(state="value", items=("a", "b"))


# ── _site_cells ───────────────────────────────────────────────────────


def _live_page(**overrides: object) -> LivePage:
    defaults: dict[str, object] = dict(
        url="https://example.com/a",
        status=200,
        error=None,
        signals=PageSignals(title="T", description="D", h1="H"),
        headings=(),
        sections=(),
    )
    defaults.update(overrides)
    return LivePage(**defaults)  # type: ignore[arg-type]


def test_site_cells_absent_for_unreachable_page() -> None:
    page = LivePage(
        url="x", status=None, error="connection_error", signals=None, headings=(), sections=()
    )
    cells = _site_cells(page, frozenset())
    assert cells.title.state == "absent"


def test_site_cells_value_and_empty_track_page_signals() -> None:
    page = _live_page(signals=PageSignals(title="T", description="", h1=None))
    cells = _site_cells(page, frozenset())
    assert cells.title == Cell(state="value", text="T")
    assert cells.description.state == "empty"
    assert cells.h1.state == "absent"


def test_site_keyword_coverage_reports_found_and_missing() -> None:
    page = _live_page(
        signals=PageSignals(
            title="Smash Burgers", description="", h1="", visible_text="crispy juicy kosher burger"
        )
    )
    cell = _site_keyword_coverage(page, frozenset({"kosher burger", "vegan tacos"}))
    assert cell.state == "value"
    assert any(i.startswith("✓") and "kosher" in i for i in cell.items)
    assert any(i.startswith("✗") and "vegan" in i for i in cell.items)


def test_site_keyword_coverage_absent_when_nothing_claimed() -> None:
    assert _site_keyword_coverage(_live_page(), frozenset()).state == "absent"


# ── verdicts ─────────────────────────────────────────────────────────


def test_scalar_verdict_not_comparable_with_fewer_than_two_values() -> None:
    assert _scalar_verdict({"strategy": Cell(state="value", text="A")}).state == "not_comparable"
    assert _scalar_verdict({"strategy": Cell(state="absent")}).state == "not_comparable"


def test_scalar_verdict_match_ignores_case_and_whitespace() -> None:
    cells = {
        "framework": Cell(state="value", text="Our Smashed Burgers Menu"),
        "site": Cell(state="value", text="OUR SMASHED BURGERS MENU"),
    }
    assert _scalar_verdict(cells).state == "match"


def test_scalar_verdict_mismatch_on_real_title_drift() -> None:
    # Live SHB /our-story/ (task-91 §1.7).
    cells = {
        "framework": Cell(state="value", text="Smash House | Our Story & Kosher Burger History"),
        "site": Cell(state="value", text="Our Smash Burger History | Smash House"),
    }
    result = _scalar_verdict(cells)
    assert result.state == "mismatch"


def test_scalar_verdict_url_uses_inspector_key_not_plain_text() -> None:
    cells = {
        "strategy": Cell(state="value", text="https://example.com/a/"),
        "site": Cell(state="value", text="https://example.com/a"),  # no trailing slash
    }
    assert _scalar_verdict(cells, url=True).state == "match"


def test_keyword_verdict_match_on_strategy_framework_overlap() -> None:
    result = _keyword_verdict(
        frozenset({"kosher burger"}),
        frozenset({"kosher burger", "smash house"}),
        Cell(state="absent"),
    )
    assert result.state == "match"


def test_keyword_verdict_mismatch_when_no_overlap() -> None:
    result = _keyword_verdict(frozenset({"a"}), frozenset({"b"}), Cell(state="absent"))
    assert result.state == "mismatch"


def test_keyword_verdict_falls_back_to_site_coverage_with_one_claim_source() -> None:
    site_cell = Cell(state="value", items=("✓ kosher burger", "✗ vegan tacos"))
    result = _keyword_verdict(frozenset({"kosher burger", "vegan tacos"}), frozenset(), site_cell)
    assert result.state == "mismatch"


def test_link_verdict_mismatch_when_claimed_link_missing_from_site() -> None:
    result = _link_verdict(
        frozenset({"example.com/menu"}),
        frozenset(),
        frozenset({"example.com/about"}),
        site_known=True,
    )
    assert result.state == "mismatch"


def test_link_verdict_match_when_all_claimed_links_present() -> None:
    result = _link_verdict(
        frozenset({"example.com/menu"}),
        frozenset(),
        frozenset({"example.com/menu"}),
        site_known=True,
    )
    assert result.state == "match"


def test_link_verdict_not_comparable_with_nothing_claimed_and_site_unreachable() -> None:
    result = _link_verdict(frozenset(), frozenset(), frozenset(), site_known=False)
    assert result.state == "not_comparable"


# ── build_matrix: integration ────────────────────────────────────────


def _strategy_doc(*items: ContentItem) -> ContentDoc:
    return ContentDoc(source="strategy", doc_type="strategy_gsheet", items=items)


def test_build_matrix_produces_a_matched_row() -> None:
    strategy_item = ContentItem(
        ref="Sheet!row 2", url="https://example.com/a", title="Strategy Title"
    )
    strategy = _strategy_doc(strategy_item)
    framework_item = ContentItem(ref="fw", url="https://example.com/a", title="Framework Title")
    # `matched_strategy_key` is `_item_key(known_row)` (crosscheck.py), NOT
    # `item.ref` — live-verified on real SHB data (task-91 3.3): a naive
    # ref-based lookup silently matched zero rows.
    outcome = FrameworkOutcome(
        source=FrameworkSource(document_id="d1", document_name="Doc1", origin="strategy_link"),
        state="checked",
        matched_strategy_key=_item_key(strategy_item),
        frame_item=framework_item,
    )
    rows = build_matrix(strategy, [outcome])
    assert len(rows) == 1
    assert rows[0].kind == "matched"
    assert rows[0].strategy is not None and rows[0].framework is not None
    assert rows[0].verdicts.title.state == "mismatch"


def test_build_matrix_keeps_unmatched_strategy_rows() -> None:
    item1 = ContentItem(ref="r1", url="https://example.com/a")
    item2 = ContentItem(ref="r2", url="https://example.com/b")
    strategy = _strategy_doc(item1, item2)
    outcome = FrameworkOutcome(
        source=FrameworkSource(document_id="d1", document_name="Doc1", origin="strategy_link"),
        state="checked",
        matched_strategy_key=_item_key(item1),
        frame_item=ContentItem(ref="fw", url="https://example.com/a"),
    )
    rows = build_matrix(strategy, [outcome])
    kinds = {r.key: r.kind for r in rows}
    assert kinds["r1"] == "matched"
    assert kinds["r2"] == "strategy_only"


def test_build_matrix_keeps_framework_with_no_strategy_match() -> None:
    strategy = _strategy_doc(ContentItem(ref="r1", url="https://example.com/a"))
    outcome = FrameworkOutcome(
        source=FrameworkSource(document_id="d1", document_name="Orphan Doc", origin="drive_search"),
        state="checked",
        matched_strategy_key=None,
        frame_item=ContentItem(ref="fw", url="https://example.com/orphan"),
    )
    rows = build_matrix(strategy, [outcome])
    kinds = {r.key: r.kind for r in rows}
    assert kinds["r1"] == "strategy_only"
    assert kinds["framework:fw"] == "framework_only"


def test_build_matrix_skips_not_accessible_frameworks() -> None:
    strategy = _strategy_doc(ContentItem(ref="r1", url="https://example.com/a"))
    outcome = FrameworkOutcome(
        source=FrameworkSource(document_id="d1", document_name="Doc1", origin="drive_search"),
        state="not_accessible",
        reason="Document not accessible.",
    )
    rows = build_matrix(strategy, [outcome])
    assert len(rows) == 1
    assert rows[0].kind == "strategy_only"
