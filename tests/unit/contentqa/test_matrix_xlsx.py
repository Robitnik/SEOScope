"""contentqa/matrix_xlsx.py — Excel export of the comparison matrix (task-91 §3.5).

Uses the wire DTO (`MatrixRowDTO`), not the internal `matrix.py` dataclass
— see the module docstring for why: the export endpoint reads an already-
persisted matrix from the DB, which only ever comes back as this DTO.
"""

from __future__ import annotations

import io

from openpyxl import load_workbook

from app.contentqa.matrix_xlsx import build_matrix_xlsx
from app.schemas.strategy_package import (
    MatrixCellDTO,
    MatrixFieldVerdictDTO,
    MatrixRowDTO,
    MatrixRowVerdictsDTO,
    MatrixSourceCellsDTO,
)

_ABSENT = MatrixCellDTO(state="absent")


def _cells(**overrides: MatrixCellDTO) -> MatrixSourceCellsDTO:
    base: dict[str, MatrixCellDTO] = dict(
        url=_ABSENT, title=_ABSENT, description=_ABSENT, h1=_ABSENT,
        keywords=_ABSENT, links=_ABSENT,
    )
    base.update(overrides)
    return MatrixSourceCellsDTO(**base)


def _verdicts(**overrides: MatrixFieldVerdictDTO) -> MatrixRowVerdictsDTO:
    na = MatrixFieldVerdictDTO(state="not_comparable")
    base: dict[str, MatrixFieldVerdictDTO] = dict(
        url=na, title=na, description=na, h1=na, keywords=na, links=na,
    )
    base.update(overrides)
    return MatrixRowVerdictsDTO(**base)


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
    assert workbook.sheetnames == ["Comparison", "Mismatches"]


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
    summary = ws.cell(1, 2).value
    assert "URL:✓" in summary
    assert "Title:✗" in summary


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
    assert workbook.sheetnames == ["Comparison", "Mismatches"]
