"""Build one portfolio-wide XLSX from a batch's client runs (task-50): one
sheet per client, the same metrics table + historical table `xlsx.py`
already writes across two separate sheets per single-client run, stacked on
ONE sheet per client here — matching the agency's own hand-kept multi-tab
workbook this replaces (`SWM`/`GRP`/`EFFYS`/... tabs, each with both tables
one under the other).

Deliberately excludes that workbook's `Summary` tab (a scorecard column plus
human-written "Action Plan"/"Client Bullet Points" narrative per client) —
the narrative half needs offline context (audits in progress, site changes)
no metric can supply, and the user's own call was to leave the whole tab out
rather than ship a half-mechanical one.
"""

from __future__ import annotations

from pathlib import Path

from openpyxl import Workbook

from app.reports.table import ReportTable
from app.reports.xlsx import write_historical_table, write_metrics_table

_SHEET_NAME_LIMIT = 31  # Excel's own hard cap on a sheet's title length.


class NoClientDataError(RuntimeError):
    """None of the batch's children have a finished result to export yet."""


def _unique_sheet_name(used: set[str], name: str) -> str:
    base = (name.strip() or "Client")[:_SHEET_NAME_LIMIT]
    candidate = base
    suffix_n = 2
    while candidate in used:
        suffix = f" ({suffix_n})"
        candidate = base[: _SHEET_NAME_LIMIT - len(suffix)] + suffix
        suffix_n += 1
    used.add(candidate)
    return candidate


def build_portfolio_xlsx(rows: list[tuple[str, ReportTable]], out_path: Path) -> Path:
    """`rows` is `(client_name, table)` in the order sheets should appear —
    the caller already filtered out clients still `queued`/`running` or
    without a `result_json` to reconstruct a table from.
    """
    if not rows:
        raise NoClientDataError("No client results available to export")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    wb = Workbook()
    wb.remove(wb.active)  # drop the default blank "Sheet" — every tab here is a real client
    used_names: set[str] = set()
    for client_name, table in rows:
        ws = wb.create_sheet(_unique_sheet_name(used_names, client_name))
        next_row = write_metrics_table(ws, client_name, table, start_row=1)
        write_historical_table(ws, client_name, table.history, start_row=next_row + 1)
        ws.freeze_panes = "B3"

    wb.save(out_path)
    return out_path
