"""readers/gsheet.py — Sheets API grid -> ContentDoc parity with the xlsx
reader, plus `find_framework_links` (task-90 §1.8/§2.2/§4.2).

Parity is the point: `read_strategy_gsheet` and `read_strategy_xlsx` share
`readers/tabular.py`'s core, so the same logical content built through
either source must produce equivalent items — this is what proves the
task-90 §4.2 refactor is behavior-preserving, not just "still compiles".
"""

from __future__ import annotations

from app.contentqa.readers.gsheet import find_framework_links, read_strategy_gsheet
from app.integrations.google_api.sheets_client import CellLink, SheetCell, SheetGrid

_GOOGLE_DOC_MIME = "application/vnd.google-apps.kix"


def _cell(text: str | None, links: tuple[CellLink, ...] = ()) -> SheetCell:
    return SheetCell(formatted_value=text, links=links)


def _row(*values: str | None) -> tuple[SheetCell, ...]:
    return tuple(_cell(v) for v in values)


def _strategy_grid() -> tuple[SheetGrid, ...]:
    """Same logical content as `test_readers_xlsx.py::_build_workbook`, built
    as a Sheets API grid instead of an openpyxl workbook."""
    sheet1 = SheetGrid(
        title="Q3 Strategy",
        index=0,
        rows=(
            _row("Q3 2026 Strategy"),
            _row(
                "Page Status", "Intent", "Page Name", "URL", "New URL",
                "Primary Key", "Secondary Key", "Longtail Key", "H1", "Title", "Description",
            ),
            _row(
                "OPTIMIZE PAGE", "Navigational", "Homepage",
                "https://example.com/", "https://example.com/",
                "building surveying software", "survey reporting software",
                "RICS compliant survey software",
                "Building Surveying Software for Digital Capture, Reporting and Analysis",
                "Building Surveying Software UK | Digital Reports and RICS Templates | GoReport",
                "GoReport is UK building surveying software for mobile capture and reporting.",
            ),
            _row(
                "OPTIMIZE PAGE", "Commercial", "Pricing",
                "https://example.com/pricing/", "https://example.com/pricing/",
                "survey software pricing", "", "",
                "Survey Software Pricing", "Survey Software Pricing | GoReport",
                "See plans and pricing for our survey software.",
            ),
        ),
    )
    sheet2 = SheetGrid(
        title="Structure v 2.0",
        index=1,
        rows=(
            _row("Pages", "URL", "Recommended URL (redirect to...)", "Breadcrumbs", "Comments"),
            _row("Home", "https://example.com/", "https://example.com/", "Home", "Q3 #1"),
        ),
    )
    return (sheet1, sheet2)


def test_column_mapping_by_header_alias_matches_xlsx_reader() -> None:
    doc = read_strategy_gsheet(_strategy_grid(), source="test-sheet")
    home = doc.items[0]
    assert home.page_name == "Homepage"
    assert home.url == "https://example.com/"
    assert home.intent == "Navigational"
    expected_title = (
        "Building Surveying Software UK | Digital Reports and RICS Templates | GoReport"
    )
    assert home.title == expected_title
    assert home.h1 == "Building Surveying Software for Digital Capture, Reporting and Analysis"
    assert home.keywords == (
        "building surveying software", "survey reporting software",
        "RICS compliant survey software",
    )


def test_ref_carries_sheet_name_and_row_number() -> None:
    doc = read_strategy_gsheet(_strategy_grid(), source="test-sheet")
    assert doc.items[0].ref == "Q3 Strategy!row 3"
    assert doc.items[1].ref == "Q3 Strategy!row 4"


def test_second_sheet_without_title_description_is_skipped() -> None:
    doc = read_strategy_gsheet(_strategy_grid(), source="test-sheet")
    assert "Structure v 2.0" in doc.skipped_sheets


def test_sheets_filter_limits_items_and_marks_the_rest_not_selected() -> None:
    doc = read_strategy_gsheet(_strategy_grid(), source="test-sheet", sheets=["Q3 Strategy"])
    assert len(doc.items) == 2
    states = {s.name: s.state for s in doc.sheet_states}
    assert states["Structure v 2.0"] == "not_selected"


def test_chip_is_the_primary_link_form_found() -> None:
    """Live: 148 of 209 real links are chips, not hyperlinks (task-90 §1.8).
    A cell whose ONLY link is a chip must still surface a candidate."""
    chip = CellLink(
        form="chip",
        uri="https://docs.google.com/document/d/1Bnu1wqs9HidtVJmu2K3Fbs__bWD79_Vr96VBMlYQTog",
        mime_type=_GOOGLE_DOC_MIME,
    )
    grid = (
        SheetGrid(
            title="Q3-Q4 2026 Strategy", index=0,
            rows=((_cell("SHB_FRAMEWORK_HOME_PAGE_082726", links=(chip,)),),),
        ),
    )
    candidates = find_framework_links(grid)
    assert len(candidates) == 1
    c = candidates[0]
    assert c.sheet == "Q3-Q4 2026 Strategy"
    assert c.row == 1
    assert c.col == 0
    assert c.form == "chip"
    assert c.document_id == "1Bnu1wqs9HidtVJmu2K3Fbs__bWD79_Vr96VBMlYQTog"
    assert c.display_text == "SHB_FRAMEWORK_HOME_PAGE_082726"


def test_hyperlink_form_is_also_found() -> None:
    link = CellLink(form="hyperlink", uri="https://docs.google.com/document/d/abc123")
    grid = (SheetGrid(title="Sheet1", index=0, rows=((_cell("x", links=(link,)),),)),)
    candidates = find_framework_links(grid)
    assert len(candidates) == 1
    assert candidates[0].form == "hyperlink"
    assert candidates[0].document_id == "abc123"


def test_chip_pointing_to_a_non_doc_is_not_a_framework_candidate() -> None:
    """A chip's `mime_type` filters out Sheets/Slides/PDF targets without a
    round-trip to Drive (task-90 §1.8) — only a Google Doc is a framework."""
    sheet_chip = CellLink(
        form="chip",
        uri="https://docs.google.com/spreadsheets/d/xyz",
        mime_type="application/vnd.google-apps.spreadsheet",
    )
    grid = (SheetGrid(title="Sheet1", index=0, rows=((_cell("x", links=(sheet_chip,)),),)),)
    assert find_framework_links(grid) == ()


def test_non_doc_url_is_not_a_framework_candidate() -> None:
    link = CellLink(form="hyperlink", uri="https://example.com/some-page")
    grid = (SheetGrid(title="Sheet1", index=0, rows=((_cell("x", links=(link,)),),)),)
    assert find_framework_links(grid) == ()


def test_multiple_candidates_in_different_rows_are_all_returned() -> None:
    chip1 = CellLink(
        form="chip", uri="https://docs.google.com/document/d/doc1", mime_type=_GOOGLE_DOC_MIME
    )
    chip2 = CellLink(
        form="chip", uri="https://docs.google.com/document/d/doc2", mime_type=_GOOGLE_DOC_MIME
    )
    grid = (
        SheetGrid(
            title="Blogs", index=0,
            rows=(
                (_cell("a", links=(chip1,)),),
                (_cell("b", links=(chip2,)),),
            ),
        ),
    )
    candidates = find_framework_links(grid)
    assert {c.document_id for c in candidates} == {"doc1", "doc2"}
    assert {c.row for c in candidates} == {1, 2}


def test_no_links_returns_empty_tuple() -> None:
    grid = (SheetGrid(title="Sheet1", index=0, rows=((_cell("plain text"),),)),)
    assert find_framework_links(grid) == ()
