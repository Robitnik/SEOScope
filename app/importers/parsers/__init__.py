"""Shared types for versioned AISeoTools artifact parsers (task-07).

Each parser module exposes `parse(files: dict[str, Path], *, fallback_date:
date) -> list[ParsedArtifact] | ParseSkip`, operating only on the files the
scanner already matched for one run directory/flat file — no directory
walking, no DB access, matching how `app.metrics.normalize.normalize_result`
stays pure. A list is returned (never a bare `ParsedArtifact`) because one
source artifact can legitimately produce snapshots for more than one
provider (the client-report XLSX mixes GSC and Reporting Ninja metrics in
one sheet); a parser must never return an empty list — use `ParseSkip`
instead.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Literal

from app.schemas.adapter import MetricValueResult
from app.schemas.enums import Provider, Quality, SourceMode


@dataclass(frozen=True)
class ParsedArtifact:
    """One importable snapshot's worth of metrics from a single artifact,
    shaped like a slimmed-down `app.schemas.adapter.CollectionResult` so the
    write path can feed it straight into `normalize_result`.
    """

    provider: Provider
    source_mode: SourceMode
    quality: Quality
    observed_at: datetime
    metrics: list[MetricValueResult]
    data_through: date | None = None
    warnings: list[str] = field(default_factory=list)
    provenance: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ParseSkip:
    """The artifact was recognized but produces no importable metrics.

    `level="info"` for artifacts that are out of scope by design (rank
    movement, an empty run directory); `level="warning"` (the default) for
    real data that can't be imported without guessing (no exact date range,
    unknown schema).
    """

    reason: str
    level: Literal["info", "warning"] = "warning"
