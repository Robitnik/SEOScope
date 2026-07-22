"""Read-only scan of an AISeoTools `--source` tree (task-07): recognizes
dated run directories and the two flat-file artifact patterns under `out/`,
and skips everything else (cache dirs, unrelated pipelines like
blog/framework/tech runs, undated junk/test directories) without opening a
single byte outside what a parser later reads.

Naming convention confirmed against the AISeoTools source
(`export_paths.py`'s `default_run_dir()`): `out/{slug}_{YYYY-MM-DD}_{mode}`
— the date is the collection/run date, not necessarily a data period.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

_DIR_PATTERN = re.compile(
    r"^(?P<slug>.+?)_(?P<date>\d{4}-\d{2}-\d{2})(?:_(?P<suffix>[A-Za-z0-9]+))?$"
)
_CLIENT_REPORT_PATTERN = re.compile(
    r"^client_report_(?P<code>.+)_(?P<date>\d{4}-\d{2}-\d{2})\.xlsx$"
)
_SEMRUSH_FLAT_PATTERN = re.compile(r"^semrush_(?P<domain>.+)_(?P<date>\d{4}-\d{2}-\d{2})\.json$")

# Dir-suffix -> importer "kind". Every other suffix (framework/blog/tech/...)
# and every un-suffixed legacy dir is intentionally not in this map: those
# pipelines aren't in the 4-provider catalog this importer backfills.
_DIR_SUFFIX_KIND = {"ahrefs": "ahrefs", "gsc": "gsc", "rankmove": "rank_movement"}


@dataclass(frozen=True)
class RunArtifact:
    kind: str  # "ahrefs" | "gsc" | "semrush" | "rank_movement" | "client_report"
    relative_path: str  # primary file's path relative to --source, for reporting/hashing
    files: dict[str, Path]  # canonical logical filename -> absolute Path
    domain_or_code: str  # raw slug/code as it appears on disk, unnormalized
    run_date: date  # the run/collection date from the dir or file name


def _parse_run_date(value: str) -> date | None:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None


def _scan_dir(entry: Path, source_root: Path) -> RunArtifact | None:
    match = _DIR_PATTERN.match(entry.name)
    if not match:
        return None
    suffix = match.group("suffix")
    kind = _DIR_SUFFIX_KIND.get(suffix or "")
    if kind is None:
        return None
    run_date = _parse_run_date(match.group("date"))
    if run_date is None:
        return None

    files: dict[str, Path] = {}
    if kind == "ahrefs":
        candidate = entry / "ahrefs_findings_machine.json"
        if candidate.is_file():
            files["ahrefs_findings_machine.json"] = candidate
    elif kind == "gsc":
        machine = entry / "gsc_findings_machine.json"
        chart = entry / "performance_chart.csv"
        if machine.is_file():
            files["gsc_findings_machine.json"] = machine
        elif chart.is_file():
            files["performance_chart.csv"] = chart
    elif kind == "rank_movement":
        candidate = entry / "rank_movement_machine.json"
        if candidate.is_file():
            files["rank_movement_machine.json"] = candidate

    if not files:
        return None  # e.g. an empty run dir — nothing was ever produced

    primary = next(iter(files.values()))
    return RunArtifact(
        kind=kind,
        relative_path=str(primary.relative_to(source_root)),
        files=files,
        domain_or_code=match.group("slug"),
        run_date=run_date,
    )


def _scan_flat_file(entry: Path, source_root: Path) -> RunArtifact | None:
    if match := _CLIENT_REPORT_PATTERN.match(entry.name):
        run_date = _parse_run_date(match.group("date"))
        if run_date is None:
            return None
        return RunArtifact(
            kind="client_report",
            relative_path=str(entry.relative_to(source_root)),
            files={"client_report.xlsx": entry},
            domain_or_code=match.group("code"),
            run_date=run_date,
        )
    if match := _SEMRUSH_FLAT_PATTERN.match(entry.name):
        run_date = _parse_run_date(match.group("date"))
        if run_date is None:
            return None
        return RunArtifact(
            kind="semrush",
            relative_path=str(entry.relative_to(source_root)),
            files={"semrush_snapshot.json": entry},
            domain_or_code=match.group("domain"),
            run_date=run_date,
        )
    return None


def scan_source(source_root: Path) -> Iterator[RunArtifact]:
    """Yield every recognized artifact one level under `<source_root>/out/`.

    Never writes, never opens a file outside what a later parser call reads
    for the SAME artifact — the DoD's `--source` read-only guarantee.
    """
    out_dir = source_root / "out"
    if not out_dir.is_dir():
        return
    for entry in sorted(out_dir.iterdir()):
        if entry.name.startswith("."):
            # .rankmove_cache/, .gsc_cache/, .serp_cache/, .ahrefs_cache/ — caches, not artifacts.
            continue
        if entry.is_dir():
            artifact = _scan_dir(entry, source_root)
        elif entry.is_file():
            artifact = _scan_flat_file(entry, source_root)
        else:
            artifact = None
        if artifact is not None:
            yield artifact
