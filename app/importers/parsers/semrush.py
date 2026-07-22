"""Semrush domain snapshot parser (task-07): `semrush_<domain>_<date>.json`,
a flat file directly under `out/`, not in a dated subdirectory.

Semrush's own UI has no export for Authority Score, and domain-level
organic traffic/keywords only exist when a separate `sr_overview.csv`
export succeeded — both are frequently `null` in real data, which is not
an ingest error. `value_numeric=None` is emitted per-metric rather than
skipping the whole artifact, so `referring_domains` (usually populated)
still gets imported even when the others are not.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.importers.parsers import ParsedArtifact, ParseSkip
from app.schemas.adapter import MetricValueResult
from app.schemas.enums import MetricUnit, PeriodRole, Provider, Quality, SourceMode

IMPORTER_VERSION = "semrush_snapshot.v1"


def _snapshot_metric(key: str, value: Any, unit: MetricUnit) -> MetricValueResult:
    return MetricValueResult(
        metric_key=key,
        period_role=PeriodRole.snapshot,
        value_numeric=Decimal(str(value)) if value is not None else None,
        unit=unit,
        quality=Quality.complete if value is not None else Quality.partial,
    )


def parse(files: dict[str, Path], *, fallback_date: date) -> list[ParsedArtifact] | ParseSkip:
    path = files.get("semrush_snapshot.json")
    if path is None:
        return ParseSkip(reason="semrush snapshot JSON not found", level="info")

    try:
        raw: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return ParseSkip(reason=f"unreadable/invalid JSON: {exc}")

    if "backlinks_overview" not in raw and "overview" not in raw:
        return ParseSkip(
            reason="unknown schema: neither 'overview' nor 'backlinks_overview' present"
        )

    overview = raw.get("overview")
    if not isinstance(overview, dict):
        overview = {}
    backlinks = raw.get("backlinks_overview")
    if not isinstance(backlinks, dict):
        backlinks = {}

    authority_score = overview.get("authority_score")
    if authority_score is None:
        authority_score = backlinks.get("ascore")

    metrics = [
        _snapshot_metric("semrush_authority_score", authority_score, MetricUnit.score),
        _snapshot_metric(
            "semrush_organic_traffic", overview.get("organic_traffic"), MetricUnit.count
        ),
        _snapshot_metric(
            "semrush_organic_keywords", overview.get("organic_keywords"), MetricUnit.count
        ),
        _snapshot_metric(
            "semrush_referring_domains", backlinks.get("domains_num"), MetricUnit.count
        ),
    ]

    warnings = [str(w) for w in (raw.get("warnings") or []) if isinstance(w, str)]
    if authority_score is None:
        warnings.append(
            "authority_score unavailable (no Semrush UI export for it; "
            "overview/backlinks_overview both missing it)"
        )

    date_str = raw.get("date") or ""
    observed_at: datetime | None = None
    try:
        observed_at = (
            datetime.combine(date.fromisoformat(date_str), datetime.min.time(), tzinfo=UTC)
            if date_str
            else None
        )
    except ValueError:
        observed_at = None
    if observed_at is None:
        observed_at = datetime.combine(fallback_date, datetime.min.time(), tzinfo=UTC)
        warnings.append(
            "no valid 'date' field in file; observed_at falls back to the run directory's date"
        )

    quality = (
        Quality.complete if all(m.value_numeric is not None for m in metrics) else Quality.partial
    )

    return [
        ParsedArtifact(
            provider=Provider.semrush,
            source_mode=SourceMode.browser,
            quality=quality,
            observed_at=observed_at,
            metrics=metrics,
            warnings=warnings,
            provenance={"domain": raw.get("domain", ""), "database": raw.get("database", "")},
        )
    ]
