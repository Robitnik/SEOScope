"""Sheets API client — grid data with hyperlinks, rich-text link runs AND
smart chips (task-90 §1.5/§1.5a/§1.8).

Two facts measured live on real strategy workbooks, both load-bearing:

1. `includeGridData=true` WITHOUT a `fields` mask returns the entire
   rendered grid — measured at **136 MB** for one real strategy (task-90
   §1.5a). The masks below are mandatory, not an optimisation.
2. `fields` masks reject nested subpaths (`format/link/uri`) inside
   `values(...)` with `400 INVALID_ARGUMENT` — measured live. The masks stay
   flat; filtering happens in Python (`_parse_cell`).

`chipRuns` is the field this project first shipped without, and it was
wrong: the "Framework / Copy Link" column in every live strategy is
populated with an `@`-mention smart chip, not a hyperlink or a plain URL.
Its target lives ONLY in `chipRuns[].chip.richLinkProperties.{uri,mimeType}`
— omitting it from the mask silently undercounts real links (a first scan of
this project's own strategies missed 148 of 209 real links this way, task-90
§1.8). `mime_type` on a chip link (`application/vnd.google-apps.kix` for a
Doc) lets a caller filter candidates without a second round-trip to Drive.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from app.integrations.google_api.auth import GoogleApiSession

_SHEETS_API_BASE = "https://sheets.googleapis.com/v4"

# Flat fields only — nested subpaths 400 live (module docstring, point 2).
_GRID_FIELDS = (
    "sheets(properties(title,index),"
    "data(rowData(values("
    "formattedValue,hyperlink,textFormatRuns,chipRuns,userEnteredValue"
    "))))"
)
# Cheap link-only scan (no cell text) — 2-210 KB live vs. up to ~700 KB with
# formattedValue included, both nowhere near the 136 MB unmasked call.
_LINKS_ONLY_FIELDS = (
    "sheets(properties(title),data(rowData(values(hyperlink,textFormatRuns,chipRuns))))"
)

# `=HYPERLINK("https://...", "label")` — the one link form that only shows
# up in `userEnteredValue.formulaValue`, not in a dedicated structured field.
_FORMULA_URL_RE = re.compile(r'HYPERLINK\(\s*"([^"]+)"', re.IGNORECASE)


@dataclass(frozen=True)
class CellLink:
    form: str  # "hyperlink" | "text_run" | "chip" | "formula"
    uri: str
    mime_type: str | None = None  # chips only


@dataclass(frozen=True)
class SheetCell:
    formatted_value: str | None
    links: tuple[CellLink, ...]


@dataclass(frozen=True)
class SheetGrid:
    title: str
    index: int
    rows: tuple[tuple[SheetCell, ...], ...]


class SheetsClient:
    def __init__(self, session: GoogleApiSession) -> None:
        self._session = session

    def get_grid(self, spreadsheet_id: str, *, links_only: bool = False) -> tuple[SheetGrid, ...]:
        fields = _LINKS_ONLY_FIELDS if links_only else _GRID_FIELDS
        data = self._session.get_json(
            f"{_SHEETS_API_BASE}/spreadsheets/{spreadsheet_id}",
            params={"includeGridData": "true", "fields": fields},
        )
        sheets = data.get("sheets")
        if not isinstance(sheets, list):
            return ()
        return tuple(_parse_sheet(sh) for sh in sheets if isinstance(sh, dict))


def _parse_sheet(sheet: dict[str, Any]) -> SheetGrid:
    props = sheet.get("properties") or {}
    rows: list[tuple[SheetCell, ...]] = []
    for block in sheet.get("data", []) or []:
        for row in block.get("rowData", []) or []:
            values = row.get("values") or []
            rows.append(tuple(_parse_cell(c) for c in values if isinstance(c, dict)))
    try:
        index = int(props.get("index") or 0)
    except (TypeError, ValueError):
        index = 0
    return SheetGrid(title=str(props.get("title") or ""), index=index, rows=tuple(rows))


def _parse_cell(cell: dict[str, Any]) -> SheetCell:
    links: list[CellLink] = []

    hyperlink = cell.get("hyperlink")
    if isinstance(hyperlink, str) and hyperlink:
        links.append(CellLink(form="hyperlink", uri=hyperlink))

    for run in cell.get("textFormatRuns") or []:
        if not isinstance(run, dict):
            continue
        uri = (((run.get("format") or {}).get("link")) or {}).get("uri")
        if isinstance(uri, str) and uri:
            links.append(CellLink(form="text_run", uri=uri))

    for run in cell.get("chipRuns") or []:
        if not isinstance(run, dict):
            continue
        rich = ((run.get("chip") or {}).get("richLinkProperties")) or {}
        uri = rich.get("uri")
        if isinstance(uri, str) and uri:
            mime_type_raw = rich.get("mimeType")
            mime_type = mime_type_raw if isinstance(mime_type_raw, str) else None
            links.append(CellLink(form="chip", uri=uri, mime_type=mime_type))

    formula = (cell.get("userEnteredValue") or {}).get("formulaValue")
    if isinstance(formula, str) and "HYPERLINK" in formula.upper():
        match = _FORMULA_URL_RE.search(formula)
        if match:
            links.append(CellLink(form="formula", uri=match.group(1)))

    formatted_value = cell.get("formattedValue")
    return SheetCell(
        formatted_value=formatted_value if isinstance(formatted_value, str) else None,
        links=tuple(links),
    )
