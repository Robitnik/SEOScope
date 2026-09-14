"""contentqa/matrix_xlsx.py — Excel export of the comparison matrix (task-91 §3.5).

Uses the wire DTO (`MatrixRowDTO`), not the internal `matrix.py` dataclass
— see the module docstring for why: the export endpoint reads an already-
persisted matrix from the DB, which only ever comes back as this DTO.
"""

from __future__ import annotations

import io

from openpyxl import load_workbook

from app.contentqa.matrix_xlsx import _COLUMN_WIDTHS, _TOTAL_COLUMNS, build_matrix_xlsx
from app.schemas.strategy_package import (
    CopyRowDTO,
    CopyStatsDTO,
    CopyTabResultDTO,
    FrameworkCandidateDTO,
    FrameworkOutcomeDTO,
    LinkAuditDTO,
    MatrixCellDTO,
    MatrixFieldVerdictDTO,
    MatrixRowDTO,
    MatrixRowVerdictsDTO,
    MatrixSourceCellsDTO,
)

_ABSENT = MatrixCellDTO(state="absent")


def _cells(**overrides: MatrixCellDTO) -> MatrixSourceCellsDTO:
    base: dict[str, MatrixCellDTO] = dict(
        url=_ABSENT, title=_ABSENT, description=_ABSENT, h1=_ABSENT, keywords=_ABSENT,
    )
    base.update(overrides)
    return MatrixSourceCellsDTO(**base)


def _verdicts(**overrides: MatrixFieldVerdictDTO) -> MatrixRowVerdictsDTO:
    na = MatrixFieldVerdictDTO(state="not_comparable")
    base: dict[str, MatrixFieldVerdictDTO] = dict(
        url=na, title=na, description=na, h1=na, keywords=na,
    )
    base.update(overrides)
    return MatrixRowVerdictsDTO(**base)


def _link_audit(**overrides: object) -> LinkAuditDTO:
    na = MatrixFieldVerdictDTO(state="not_comparable")
    base: dict[str, object] = dict(state="checked", reason=None, in_copy=na, on_site=na)
    base.update(overrides)
    return LinkAuditDTO(**base)  # type: ignore[arg-type]


def test_build_matrix_xlsx_produces_a_valid_workbook_with_two_sheets() -> None:
    rows = [
        MatrixRowDTO(
            key="r1", kind="matched", label="https://example.com/a",
            strategy=_cells(title=MatrixCellDTO(state="value", text="Strategy Title")),
            framework=_cells(title=MatrixCellDTO(state="value", text="Framework Title")),
            site=_cells(title=MatrixCellDTO(state="value", text="Site Title")),
            verdicts=_verdicts(
                title=MatrixFieldVerdictDTO(state="mismatch", detail="strategy vs site differ")
            ),
        ),
    ]
    data = build_matrix_xlsx(rows)
    assert data[:2] == b"PK"  # xlsx is a zip archive
    workbook = load_workbook(io.BytesIO(data))
    assert workbook.sheetnames == ["Comparison", "Mismatches", "Copy"]


def _mini_table_titles(ws, max_row: int = 30) -> dict[str, int]:
    return {
        ws.cell(r, 1).value: r for r in range(1, max_row)
        if ws.cell(r, 1).value in ("Strategy", "Framework", "Site")
    }


def test_comparison_sheet_groups_by_page_three_mini_tables_in_a_row() -> None:
    """task-91 §3.6, corrected live per direct operator feedback ("10 URLs
    = 30 tables"): one page = Strategy/Framework/Site one after another,
    not one giant table per source."""
    rows = [
        MatrixRowDTO(
            key="r1", kind="matched", label="https://example.com/a",
            strategy=_cells(url=MatrixCellDTO(state="value", text="https://example.com/a")),
            framework=_cells(url=MatrixCellDTO(state="value", text="https://example.com/a")),
            site=_cells(url=MatrixCellDTO(state="value", text="https://example.com/a")),
            verdicts=_verdicts(url=MatrixFieldVerdictDTO(state="match")),
        ),
    ]
    data = build_matrix_xlsx(rows)
    ws = load_workbook(io.BytesIO(data))["Comparison"]
    assert ws.cell(1, 1).value == "https://example.com/a"  # page header, row 1
    title_rows = _mini_table_titles(ws)
    assert list(title_rows) == ["Strategy", "Framework", "Site"]
    # Each mini-table's data row (2 rows below its own title: title,
    # header, data) carries this page's URL in its own URL column.
    for title_row in title_rows.values():
        assert ws.cell(title_row + 2, 1).value == "https://example.com/a"


def test_page_header_summarizes_verdicts() -> None:
    rows = [
        MatrixRowDTO(
            key="r1", kind="matched", label="p1",
            strategy=_cells(), framework=_cells(), site=_cells(),
            verdicts=_verdicts(
                url=MatrixFieldVerdictDTO(state="match"),
                title=MatrixFieldVerdictDTO(state="mismatch"),
            ),
        ),
    ]
    data = build_matrix_xlsx(rows)
    ws = load_workbook(io.BytesIO(data))["Comparison"]
    # Title (row 1) is merged across the full row width (live feedback:
    # "Юрл не обрізай" — a long URL sharing its row with the summary text
    # used to get clipped by Excel). Summary (row 2) is one cell per field
    # — `_FIELDS` order is (url, title, description, h1, keywords, links)
    # — so each verdict can carry its own color (live feedback: "галочки
    # зелені. хрестики червоні", impossible in a single joined cell since
    # one cell has exactly one font).
    url_cell = ws.cell(2, 1)
    title_cell = ws.cell(2, 2)
    description_cell = ws.cell(2, 3)  # not_comparable by default -> muted, not colored
    assert url_cell.value == "URL: ✓"
    assert title_cell.value == "Title: ✗"
    assert description_cell.value == "Description: —"
    assert url_cell.font.color.rgb.endswith("1E7E34")
    assert title_cell.font.color.rgb.endswith("CC0000")
    assert description_cell.font.color.rgb.endswith("999999")


def test_page_header_links_the_framework_document() -> None:
    """Live feedback («додати посилання на фрейм» у Comparison): a third
    header line `Framework: <name>` -> Google Doc, same line the per-page
    Copy sheets carry. A `strategy_only` row has no framework and gets no
    line at all — its first mini-table starts right after the summary."""
    rows = [
        MatrixRowDTO(
            key="r1", kind="matched", label="https://example.com/a",
            strategy=_cells(), framework=_cells(), site=_cells(), verdicts=_verdicts(),
            framework_document_id="doc123", framework_document_name="EFFYS_PAGE_FRAMEWORK_A",
        ),
        MatrixRowDTO(
            key="r2", kind="strategy_only", label="Sheet!row 9",
            strategy=_cells(), framework=None, site=None, verdicts=_verdicts(),
        ),
    ]
    ws = load_workbook(io.BytesIO(build_matrix_xlsx(rows)))["Comparison"]
    assert ws.cell(1, 1).hyperlink.target == "https://example.com/a"  # URL title is a link
    link_cell = ws.cell(3, 1)
    assert link_cell.value == "Framework: EFFYS_PAGE_FRAMEWORK_A"
    assert link_cell.hyperlink.target == "https://docs.google.com/document/d/doc123/edit"
    assert ws.cell(4, 1).value == "Strategy"  # first mini-table follows the link line
    # second page (strategy_only): title, summary, then straight to "Strategy"
    second_title = next(
        r for r in range(5, ws.max_row + 1) if ws.cell(r, 1).value == "Sheet!row 9"
    )
    assert ws.cell(second_title + 2, 1).value == "Strategy"


def test_absent_cell_renders_the_no_such_column_marker() -> None:
    rows = [
        MatrixRowDTO(
            key="r1", kind="strategy_only", label="Sheet!row 2",
            strategy=_cells(url=_ABSENT), framework=None, site=None,
            verdicts=_verdicts(),
        ),
    ]
    data = build_matrix_xlsx(rows)
    ws = load_workbook(io.BytesIO(data))["Comparison"]
    strategy_title_row = _mini_table_titles(ws)["Strategy"]
    # title row + header row + data row -> URL is the first data column.
    assert ws.cell(strategy_title_row + 2, 1).value == "— (no such column)"


def test_missing_source_side_renders_not_in_this_run() -> None:
    rows = [
        MatrixRowDTO(
            key="r1", kind="strategy_only", label="Sheet!row 2",
            strategy=_cells(url=MatrixCellDTO(state="value", text="https://example.com/a")),
            framework=None, site=None, verdicts=_verdicts(),
        ),
    ]
    data = build_matrix_xlsx(rows)
    ws = load_workbook(io.BytesIO(data))["Comparison"]
    framework_title_row = _mini_table_titles(ws)["Framework"]
    assert ws.cell(framework_title_row + 2, 1).value == "— not in this run"


def test_mismatches_sheet_lists_only_mismatched_fields() -> None:
    rows = [
        MatrixRowDTO(
            key="r1", kind="matched", label="page-a",
            strategy=_cells(), framework=_cells(), site=_cells(),
            verdicts=_verdicts(
                title=MatrixFieldVerdictDTO(state="mismatch", detail="title drift"),
                url=MatrixFieldVerdictDTO(state="match"),
            ),
        ),
        MatrixRowDTO(
            key="r2", kind="matched", label="page-b",
            strategy=_cells(), framework=_cells(), site=_cells(),
            verdicts=_verdicts(),  # all not_comparable — no mismatch
        ),
    ]
    data = build_matrix_xlsx(rows)
    ws = load_workbook(io.BytesIO(data))["Mismatches"]
    rows_out = [
        (ws.cell(r, 1).value, ws.cell(r, 2).value, ws.cell(r, 3).value) for r in (2, 3)
    ]
    assert rows_out[0] == ("page-a", "Title", "title drift")
    assert rows_out[1] == (None, None, None)  # page-b contributes nothing


def test_mismatches_sheet_is_honest_when_nothing_mismatched() -> None:
    rows = [
        MatrixRowDTO(
            key="r1", kind="matched", label="page-a",
            strategy=_cells(), framework=_cells(), site=_cells(), verdicts=_verdicts(),
        ),
    ]
    data = build_matrix_xlsx(rows)
    ws = load_workbook(io.BytesIO(data))["Mismatches"]
    assert ws.cell(2, 1).value == "No mismatches in this run."


def test_empty_matrix_still_produces_a_valid_workbook() -> None:
    data = build_matrix_xlsx([])
    workbook = load_workbook(io.BytesIO(data))
    assert workbook.sheetnames == ["Comparison", "Mismatches", "Copy"]


def test_mismatched_field_is_bold_red_in_every_mini_table() -> None:
    """Live feedback ("Файл дуже важко читабельний"): a mismatch must be
    visible directly on the value cells in all three mini-tables, not only
    in the page header's ✓/✗ summary line."""
    rows = [
        MatrixRowDTO(
            key="r1", kind="matched", label="p1",
            strategy=_cells(title=MatrixCellDTO(state="value", text="A")),
            framework=_cells(title=MatrixCellDTO(state="value", text="B")),
            site=_cells(title=MatrixCellDTO(state="value", text="C")),
            verdicts=_verdicts(title=MatrixFieldVerdictDTO(state="mismatch")),
        ),
    ]
    data = build_matrix_xlsx(rows)
    ws = load_workbook(io.BytesIO(data))["Comparison"]
    title_rows = _mini_table_titles(ws)
    for source_title_row in title_rows.values():
        data_cell = ws.cell(source_title_row + 2, 2)  # Title is column 2
        assert data_cell.font.bold is True
        assert data_cell.font.color.rgb.endswith("CC0000")
        assert data_cell.fill.fgColor.rgb.endswith("FDEAEA")


def test_matching_field_is_bold_green() -> None:
    rows = [
        MatrixRowDTO(
            key="r1", kind="matched", label="p1",
            strategy=_cells(url=MatrixCellDTO(state="value", text="https://a")),
            framework=_cells(url=MatrixCellDTO(state="value", text="https://a")),
            site=_cells(url=MatrixCellDTO(state="value", text="https://a")),
            verdicts=_verdicts(url=MatrixFieldVerdictDTO(state="match")),
        ),
    ]
    data = build_matrix_xlsx(rows)
    ws = load_workbook(io.BytesIO(data))["Comparison"]
    strategy_title_row = _mini_table_titles(ws)["Strategy"]
    data_cell = ws.cell(strategy_title_row + 2, 1)  # URL is column 1
    assert data_cell.font.bold is True
    assert data_cell.font.color.rgb.endswith("1E7E34")


def test_outlier_source_is_red_agreeing_sources_are_green() -> None:
    """Live feedback: Strategy and Framework agree, only Site differs ->
    Strategy/Framework read green, only Site red — not all three red."""
    rows = [
        MatrixRowDTO(
            key="r1", kind="matched", label="p1",
            strategy=_cells(title=MatrixCellDTO(state="value", text="Same Title")),
            framework=_cells(title=MatrixCellDTO(state="value", text="Same Title")),
            site=_cells(title=MatrixCellDTO(state="value", text="Different Title")),
            verdicts=_verdicts(
                title=MatrixFieldVerdictDTO(state="mismatch", outlier_sources=["site"])
            ),
        ),
    ]
    data = build_matrix_xlsx(rows)
    ws = load_workbook(io.BytesIO(data))["Comparison"]
    title_rows = _mini_table_titles(ws)
    strategy_cell = ws.cell(title_rows["Strategy"] + 2, 2)  # Title is column 2
    framework_cell = ws.cell(title_rows["Framework"] + 2, 2)
    site_cell = ws.cell(title_rows["Site"] + 2, 2)
    assert strategy_cell.font.color.rgb.endswith("1E7E34")
    assert framework_cell.font.color.rgb.endswith("1E7E34")
    assert site_cell.font.color.rgb.endswith("CC0000")


def test_comparison_sheet_column_widths_match_the_real_column_count() -> None:
    """A live-caught bug (task-91 §3.5): the width list once had 7 entries
    for what was then a 6-column table (a leftover "Page" lead column
    dropped when the sheet moved to per-page grouping), silently shifting
    every column one slot to the right. Now 5 scalar fields + N link
    sub-columns (task-91 §3.6, live feedback) — the guard generalizes."""
    assert len(_COLUMN_WIDTHS) == _TOTAL_COLUMNS


def test_link_chain_columns_only_carry_a_value_on_the_framework_row() -> None:
    """task-95 §2.7: `In copy`/`On site` replaced the old per-link
    sub-columns. The chain has no "Strategy side"/"Site side" — only the
    Framework mini-table shows the real verdict text; Strategy/Site show
    a plain "—" (task-95 §2.5: the row-level `links` object has no
    per-source shape to draw from for those two blocks)."""
    rows = [
        MatrixRowDTO(
            key="r1", kind="matched", label="p1",
            strategy=_cells(), framework=_cells(), site=_cells(), verdicts=_verdicts(),
            links=_link_audit(
                in_copy=MatrixFieldVerdictDTO(
                    state="match", detail="2/2 framework link(s) in copy"
                ),
                on_site=MatrixFieldVerdictDTO(
                    state="mismatch", detail="1/2 copy link(s) on the live page"
                ),
            ),
            framework_document_id="doc1",
        ),
    ]
    data = build_matrix_xlsx(rows)
    ws = load_workbook(io.BytesIO(data))["Comparison"]
    titles = _mini_table_titles(ws)
    strategy_row = titles["Strategy"] + 2
    framework_row = titles["Framework"] + 2
    site_row = titles["Site"] + 2
    # Columns 1-5 are the scalar fields; "In copy"/"On site" are 6/7.
    assert ws.cell(strategy_row, 6).value == "—"
    assert ws.cell(strategy_row, 7).value == "—"
    assert ws.cell(framework_row, 6).value == "✓ 2/2 framework link(s) in copy"
    assert ws.cell(framework_row, 7).value == "✗ 1/2 copy link(s) on the live page"
    assert ws.cell(site_row, 6).value == "—"
    assert ws.cell(site_row, 7).value == "—"


def test_link_chain_not_comparable_shows_the_reason_not_a_symbol() -> None:
    rows = [
        MatrixRowDTO(
            key="r1", kind="matched", label="p1",
            strategy=_cells(), framework=_cells(), site=_cells(), verdicts=_verdicts(),
            links=_link_audit(
                in_copy=MatrixFieldVerdictDTO(
                    state="not_comparable", detail="no copy tab in this document"
                ),
            ),
        ),
    ]
    data = build_matrix_xlsx(rows)
    ws = load_workbook(io.BytesIO(data))["Comparison"]
    framework_row = _mini_table_titles(ws)["Framework"] + 2
    assert ws.cell(framework_row, 6).value == "no copy tab in this document"


def test_link_chain_absent_row_shows_dashes_everywhere() -> None:
    """`links=None` — matrices persisted before task-95, or a
    `strategy_only` row with no framework at all."""
    rows = [
        MatrixRowDTO(
            key="r1", kind="matched", label="p1",
            strategy=_cells(), framework=_cells(), site=_cells(), verdicts=_verdicts(),
            links=None,
        ),
    ]
    data = build_matrix_xlsx(rows)
    ws = load_workbook(io.BytesIO(data))["Comparison"]
    framework_row = _mini_table_titles(ws)["Framework"] + 2
    assert ws.cell(framework_row, 6).value == "—"
    assert ws.cell(framework_row, 7).value == "—"


def test_mismatches_sheet_includes_link_chain_mismatches() -> None:
    rows = [
        MatrixRowDTO(
            key="r1", kind="matched", label="https://example.com/a",
            strategy=_cells(), framework=_cells(), site=_cells(), verdicts=_verdicts(),
            links=_link_audit(
                on_site=MatrixFieldVerdictDTO(
                    state="mismatch", detail="1/2 copy link(s) on the live page"
                ),
            ),
        ),
    ]
    ws = load_workbook(io.BytesIO(build_matrix_xlsx(rows)))["Mismatches"]
    values = [tuple(ws.cell(r, c).value for c in (1, 2, 3)) for r in range(2, 4)]
    assert (
        "https://example.com/a", "On site", "1/2 copy link(s) on the live page",
    ) in values


def test_title_and_description_show_a_character_count() -> None:
    """Live feedback ("Додай ще довжини тайтлу та дескріпшина"): SERP
    truncation is a length problem independent of match/mismatch, so the
    char count is appended regardless of verdict — but only for the two
    fields it was asked for, not every scalar column (URL/H1 stay as-is)."""
    rows = [
        MatrixRowDTO(
            key="r1", kind="matched", label="p1",
            strategy=_cells(
                url=MatrixCellDTO(state="value", text="https://a"),
                title=MatrixCellDTO(state="value", text="Twelve chars"),
                description=MatrixCellDTO(state="value", text="A description of 26 chars."),
                h1=MatrixCellDTO(state="value", text="Heading"),
            ),
            framework=_cells(), site=_cells(), verdicts=_verdicts(),
        ),
    ]
    data = build_matrix_xlsx(rows)
    ws = load_workbook(io.BytesIO(data))["Comparison"]
    strategy_title_row = _mini_table_titles(ws)["Strategy"]
    data_row = strategy_title_row + 2
    assert ws.cell(data_row, 2).value == "Twelve chars\n(12 chars)"
    assert ws.cell(data_row, 3).value == "A description of 26 chars.\n(26 chars)"
    assert ws.cell(data_row, 1).value == "https://a"
    assert ws.cell(data_row, 4).value == "Heading"


# ── аркуші Copy (task-92 §2.6, per-page layout after live feedback) ─────────


def _framework_outcome(
    *, document_name: str, copy_diffs: list[CopyTabResultDTO],
    page_url: str | None = "https://example.com/locations/columbus-ave/",
    document_id: str = "d1",
) -> FrameworkOutcomeDTO:
    return FrameworkOutcomeDTO(
        source=FrameworkCandidateDTO(
            document_id=document_id, document_name=document_name, origin="drive_search",
        ),
        state="checked", copy_diffs=copy_diffs, page_url=page_url,
    )


def _checked_tab(rows: list[CopyRowDTO], *, title: str = "Content") -> CopyTabResultDTO:
    missing = sum(1 for r in rows if r.state == "missing")
    changed = sum(1 for r in rows if r.state == "changed")
    match = sum(1 for r in rows if r.state == "match")
    return CopyTabResultDTO(
        tab_title=title, genre="copy", role="primary", state="checked", rows=rows,
        stats=CopyStatsDTO(match=match, changed=changed, missing=missing, coverage=1.0),
    )


_CHANGED = CopyRowDTO(
    state="changed", kind="sentence", section="FAQ",
    doc_text="Open 8 AM to 5 PM.", site_text="Open 7 AM to 5 PM.", diff_summary="8 → 7",
)
_MISSING = CopyRowDTO(state="missing", kind="sentence", section="FAQ", doc_text="Cut sentence.")
_MATCH = CopyRowDTO(state="match", kind="sentence", doc_text="Same.", site_text="Same.")
_EXTRA = CopyRowDTO(state="extra", kind="sentence", site_text="Only on site.")
_EXCLUDED = CopyRowDTO(state="excluded", kind="directive", doc_text="[IMAGE SECTION: hero]")


def test_copy_index_shows_placeholder_when_no_copy_data() -> None:
    data = build_matrix_xlsx([])
    workbook = load_workbook(io.BytesIO(data))
    assert workbook.sheetnames == ["Comparison", "Mismatches", "Copy"]
    assert workbook["Copy"].cell(2, 1).value == "No copy data in this run."


def test_copy_page_gets_its_own_sheet_named_from_the_url() -> None:
    frameworks = [_framework_outcome(
        document_name="EFFYS_PAGE_FRAMEWORK_COLUMBUS_AVE_LOCATION_06112026",
        copy_diffs=[_checked_tab([_CHANGED, _MISSING, _MATCH, _EXTRA, _EXCLUDED])],
    )]
    workbook = load_workbook(io.BytesIO(build_matrix_xlsx([], frameworks=frameworks)))
    # Sheet named by the URL's last path segment, not the 51-char doc name.
    assert workbook.sheetnames == ["Comparison", "Mismatches", "Copy", "columbus-ave"]

    ws = workbook["columbus-ave"]
    assert ws["A1"].value == "https://example.com/locations/columbus-ave/"
    assert ws["A1"].hyperlink.target == "https://example.com/locations/columbus-ave/"
    assert ws["A2"].value == "Framework: EFFYS_PAGE_FRAMEWORK_COLUMBUS_AVE_LOCATION_06112026"
    assert ws["A2"].hyperlink.target == "https://docs.google.com/document/d/d1/edit"
    assert ws["A3"].value == "Copy tab: Content · 2 issues to fix"
    assert [ws.cell(5, c).value for c in range(1, 5)] == [
        "#", "Section", "Sentence (from Copy)", "Issue",
    ]
    # Only missing + changed — match/extra/excluded never reach the sheet.
    assert ws.max_row == 7
    assert [ws.cell(6, c).value for c in range(1, 5)] == [1, "FAQ", "Open 8 AM to 5 PM.", "8 → 7"]
    assert [ws.cell(7, c).value for c in range(1, 5)] == [
        2, "FAQ", "Cut sentence.", "Not on the page",
    ]
    # No explicit row height — Excel auto-fits the wrapped sentence.
    assert ws.row_dimensions[6].height is None
    assert ws.freeze_panes == "A6"


def test_copy_index_links_page_document_and_sheet() -> None:
    frameworks = [_framework_outcome(
        document_name="Doc", copy_diffs=[_checked_tab([_MISSING])],
    )]
    ws = load_workbook(io.BytesIO(build_matrix_xlsx([], frameworks=frameworks)))["Copy"]
    assert [ws.cell(1, c).value for c in range(1, 6)] == [
        "Page", "Framework document", "Copy tab", "Issues", "Sheet",
    ]
    assert ws.cell(2, 1).hyperlink.target == "https://example.com/locations/columbus-ave/"
    assert ws.cell(2, 2).hyperlink.target == "https://docs.google.com/document/d/d1/edit"
    assert ws.cell(2, 3).value == "Content"
    assert ws.cell(2, 4).value == 1
    assert ws.cell(2, 5).value == "columbus-ave"
    assert ws.cell(2, 5).hyperlink.target == "#'columbus-ave'!A1"


def test_clean_page_is_listed_in_the_index_but_gets_no_sheet() -> None:
    """Checked-and-clean ≠ not checked (PLAN §12): the page still shows
    up, with "0 — clean", but there is nothing to fix so no sheet."""
    frameworks = [_framework_outcome(document_name="Doc", copy_diffs=[_checked_tab([_MATCH])])]
    workbook = load_workbook(io.BytesIO(build_matrix_xlsx([], frameworks=frameworks)))
    assert workbook.sheetnames == ["Comparison", "Mismatches", "Copy"]
    ws = workbook["Copy"]
    assert ws.cell(2, 4).value == "0 — clean"
    assert ws.cell(2, 5).value == "—"


def test_unchecked_tab_states_are_listed_with_their_reason() -> None:
    tab = CopyTabResultDTO(
        tab_title="Content", genre="copy", role="primary", state="no_url",
        reason="This framework has no URL to compare against.",
    )
    frameworks = [_framework_outcome(document_name="Blog", copy_diffs=[tab], page_url=None)]
    workbook = load_workbook(io.BytesIO(build_matrix_xlsx([], frameworks=frameworks)))
    assert workbook.sheetnames == ["Comparison", "Mismatches", "Copy"]
    ws = workbook["Copy"]
    assert ws.cell(2, 1).value == "Blog"  # no URL -> document name, no hyperlink
    assert ws.cell(2, 1).hyperlink is None
    assert ws.cell(2, 4).value == (
        "no URL to compare — This framework has no URL to compare against."
    )


def test_page_without_persisted_url_still_gets_a_sheet() -> None:
    """Packages persisted before `page_url` existed: honest "(no page URL)",
    never a URL guessed backwards from matrix_json."""
    frameworks = [_framework_outcome(
        document_name="Old Doc", copy_diffs=[_checked_tab([_MISSING])], page_url=None,
    )]
    workbook = load_workbook(io.BytesIO(build_matrix_xlsx([], frameworks=frameworks)))
    assert workbook.sheetnames[-1] == "Old Doc"  # falls back to the document name
    ws = workbook["Old Doc"]
    assert ws["A1"].value == "(no page URL in this run)"
    assert ws["A1"].hyperlink is None


def test_sheet_names_are_excel_safe_and_unique() -> None:
    """Excel: ≤31 chars, no []:*?/\\, case-insensitively unique — live:
    193 unique names out of 203 URLs, 8 collisions (`faq`, `menu`, `home`)."""
    long_seg = "a-very-long-slug-that-goes-well-past-thirty-one-characters"
    one = _checked_tab([_MISSING])
    frameworks = [
        _framework_outcome(document_name="A", document_id="a", copy_diffs=[one],
                           page_url="https://x.com/faq/"),
        _framework_outcome(document_name="B", document_id="b", copy_diffs=[one],
                           page_url="https://x.com/help/FAQ/"),
        _framework_outcome(document_name="C", document_id="c", copy_diffs=[one],
                           page_url=f"https://x.com/{long_seg}/"),
        _framework_outcome(document_name="D", document_id="d", copy_diffs=[one],
                           page_url="https://x.com/"),
        _framework_outcome(document_name="What: is [this]?", document_id="e",
                           copy_diffs=[_checked_tab([_MISSING])], page_url=None),
    ]
    names = load_workbook(io.BytesIO(build_matrix_xlsx([], frameworks=frameworks))).sheetnames[3:]
    assert names[0] == "faq"
    assert names[1] == "FAQ (2)"  # same name case-insensitively -> deduped
    assert len(names[2]) <= 31 and names[2].startswith("a-very-long-slug")
    assert names[3] == "home"  # bare domain root
    assert names[4] == "What- is -this-?"[:31].replace("?", "-")  # forbidden chars replaced


def test_two_checked_language_tabs_on_one_page_get_suffixed_sheets() -> None:
    sv = CopyTabResultDTO(
        tab_title="Swedish Content", genre="copy", role="primary", state="checked",
        language_hint="sv", rows=[_MISSING], stats=CopyStatsDTO(missing=1),
    )
    en = CopyTabResultDTO(
        tab_title="English Content", genre="copy", role="secondary", state="checked",
        language_hint="en", rows=[_MISSING], stats=CopyStatsDTO(missing=1),
    )
    frameworks = [_framework_outcome(
        document_name="Doc", copy_diffs=[sv, en], page_url="https://laxton.se/shop/ouija/",
    )]
    names = load_workbook(io.BytesIO(build_matrix_xlsx([], frameworks=frameworks))).sheetnames[3:]
    assert names == ["ouija (sv)", "ouija (en)"]
