"""Rank movement parser (task-07): recognized but out of scope for the
current metric catalog.

`rank_movement_machine.json` is a derived winners/losers diff between two
arbitrary comparison dates (`date_now`/`date_compared`, often months apart —
not a `period_start`/`period_end` any catalog metric compares against),
built by blending Ahrefs/Semrush keyword-position data with GSC. It isn't a
snapshot of any single provider's raw metrics, and nothing in the current
`ahrefs`/`semrush`/`gsc`/`reporting_ninja` catalog corresponds to it, so
this always returns a skip. It exists purely so the scanner has somewhere
to route `_rankmove` directories instead of silently ignoring them —
skipping here is a scope decision, not a schema failure, hence `level="info"`.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from app.importers.parsers import ParsedArtifact, ParseSkip

IMPORTER_VERSION = "rank_movement.v1"


def parse(files: dict[str, Path], *, fallback_date: date) -> list[ParsedArtifact] | ParseSkip:
    if "rank_movement_machine.json" not in files:
        return ParseSkip(reason="rank_movement_machine.json not found", level="info")
    return ParseSkip(
        reason=(
            "rank movement is a derived diff artifact (two arbitrary comparison "
            "dates, not a period) with no matching catalog metric; recognized, "
            "not imported"
        ),
        level="info",
    )
