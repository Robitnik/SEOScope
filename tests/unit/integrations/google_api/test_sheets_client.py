"""SheetsClient — grid parsing, all four link forms, the mandatory mask
(task-90 §1.5/§1.5a/§1.8)."""

from __future__ import annotations

import httpx

from app.integrations.google_api.sheets_client import SheetsClient

from .fakes import FakeGoogle


def _sheet(title: str, rows: list[list[dict[str, object]]], index: int = 0) -> dict[str, object]:
    return {
        "properties": {"title": title, "index": index},
        "data": [{"rowData": [{"values": row} for row in rows]}],
    }


def _one_cell_response(cell: dict[str, object], *, title: str = "Sheet1") -> httpx.Response:
    return httpx.Response(200, json={"sheets": [_sheet(title, [[cell]])]})


def test_get_grid_sends_fields_mask(fake_google: FakeGoogle) -> None:
    """The mask is mandatory — measured live at 136 MB per strategy without
    it (task-90 §1.5a). This test fails loudly if a future edit drops it."""
    fake_google.on("spreadsheets/sid1", httpx.Response(200, json={"sheets": []}))
    SheetsClient(fake_google.session()).get_grid("sid1")
    request = fake_google.requests[-1]
    fields = request.url.params.get("fields") or ""
    assert "chipRuns" in fields
    assert "hyperlink" in fields
    assert "textFormatRuns" in fields
    assert request.url.params.get("includeGridData") == "true"


def test_get_grid_links_only_mask_still_includes_chip_runs(fake_google: FakeGoogle) -> None:
    fake_google.on("spreadsheets/sid1", httpx.Response(200, json={"sheets": []}))
    SheetsClient(fake_google.session()).get_grid("sid1", links_only=True)
    request = fake_google.requests[-1]
    fields = request.url.params.get("fields") or ""
    assert "chipRuns" in fields
    assert "formattedValue" not in fields  # the whole point of links_only


def test_chip_run_is_the_primary_link_form(fake_google: FakeGoogle) -> None:
    """Live: 148 of 209 real "Framework / Copy Link" links are `@`-mention
    smart chips, not hyperlinks. A cell with ONLY `chipRuns` (no
    `hyperlink`) must still surface its target — this is the exact bug that
    made the first live scan under-count real links (task-90 §1.8)."""
    cell = {
        "formattedValue": "SHB_FRAMEWORK_HOME_PAGE_082726",
        "chipRuns": [
            {
                "chip": {
                    "richLinkProperties": {
                        "uri": "https://docs.google.com/document/d/1Bnu1wqs9HidtVJmu2K3Fbs__bWD79_Vr96VBMlYQTog",
                        "mimeType": "application/vnd.google-apps.kix",
                    }
                }
            }
        ],
    }
    fake_google.on("spreadsheets/sid1", _one_cell_response(cell, title="Q3-Q4 2026 Strategy"))
    grid = SheetsClient(fake_google.session()).get_grid("sid1")
    links = grid[0].rows[0][0].links
    assert len(links) == 1
    assert links[0].form == "chip"
    assert links[0].mime_type == "application/vnd.google-apps.kix"
    assert "1Bnu1wqs9HidtVJmu2K3Fbs__bWD79_Vr96VBMlYQTog" in links[0].uri


def test_plain_hyperlink_form(fake_google: FakeGoogle) -> None:
    cell = {"formattedValue": "link text", "hyperlink": "https://docs.google.com/document/d/abc123"}
    fake_google.on("spreadsheets/sid1", _one_cell_response(cell))
    grid = SheetsClient(fake_google.session()).get_grid("sid1")
    links = grid[0].rows[0][0].links
    assert len(links) == 1
    assert links[0].form == "hyperlink"
    assert links[0].uri == "https://docs.google.com/document/d/abc123"


def test_text_format_run_link_form(fake_google: FakeGoogle) -> None:
    """A rich-text link on part of a cell's text (not the whole cell) —
    lives in `textFormatRuns[].format.link.uri`."""
    cell = {
        "formattedValue": "See the framework here",
        "textFormatRuns": [
            {"startIndex": 4, "format": {"link": {"uri": "https://docs.google.com/document/d/xyz789"}}}
        ],
    }
    fake_google.on("spreadsheets/sid1", _one_cell_response(cell))
    grid = SheetsClient(fake_google.session()).get_grid("sid1")
    links = grid[0].rows[0][0].links
    assert len(links) == 1
    assert links[0].form == "text_run"
    assert links[0].uri == "https://docs.google.com/document/d/xyz789"


def test_formula_hyperlink_form(fake_google: FakeGoogle) -> None:
    formula_uri = "https://docs.google.com/document/d/formula1"
    cell = {
        "formattedValue": "Framework",
        "userEnteredValue": {"formulaValue": f'=HYPERLINK("{formula_uri}", "Framework")'},
    }
    fake_google.on("spreadsheets/sid1", _one_cell_response(cell))
    grid = SheetsClient(fake_google.session()).get_grid("sid1")
    links = grid[0].rows[0][0].links
    assert len(links) == 1
    assert links[0].form == "formula"
    assert links[0].uri == "https://docs.google.com/document/d/formula1"


def test_cell_with_multiple_link_forms_returns_all(fake_google: FakeGoogle) -> None:
    cell = {
        "formattedValue": "x",
        "hyperlink": "https://docs.google.com/document/d/aaa",
        "chipRuns": [{"chip": {"richLinkProperties": {"uri": "https://docs.google.com/document/d/bbb"}}}],
    }
    fake_google.on("spreadsheets/sid1", _one_cell_response(cell))
    grid = SheetsClient(fake_google.session()).get_grid("sid1")
    forms = {link.form for link in grid[0].rows[0][0].links}
    assert forms == {"hyperlink", "chip"}


def test_cell_with_no_link_has_empty_links(fake_google: FakeGoogle) -> None:
    cell = {"formattedValue": "plain text, no link"}
    fake_google.on("spreadsheets/sid1", _one_cell_response(cell))
    grid = SheetsClient(fake_google.session()).get_grid("sid1")
    assert grid[0].rows[0][0].links == ()
    assert grid[0].rows[0][0].formatted_value == "plain text, no link"


def test_sheet_title_and_index_preserved(fake_google: FakeGoogle) -> None:
    row = [{"formattedValue": "x"}]
    fake_google.on(
        "spreadsheets/sid1",
        httpx.Response(200, json={"sheets": [_sheet("Blogs Q3-Q4", [row], index=3)]}),
    )
    grid = SheetsClient(fake_google.session()).get_grid("sid1")
    assert grid[0].title == "Blogs Q3-Q4"
    assert grid[0].index == 3


def test_empty_workbook_returns_empty_tuple(fake_google: FakeGoogle) -> None:
    fake_google.on("spreadsheets/sid1", httpx.Response(200, json={}))
    assert SheetsClient(fake_google.session()).get_grid("sid1") == ()
