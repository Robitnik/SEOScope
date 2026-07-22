"""Ahrefs Site Audit `ahrefs_findings_machine.json` parser (task-07).

Tolerates the two real collection paths found in the AISeoTools output
tree: the (older) Ahrefs API/CSV path, where `health_score`/`crawl_date`/
`totals.urls_crawled` are populated, and the (current default, since Ahrefs
API units ran out) browser-automation path, where `health_score` is `null`,
`crawl_date` is `""`, and `totals.urls_crawled` stays 0 even though URLs
were clearly processed — `verify_summary.total` is used instead in that
case. Neither is a schema violation; both are normal, expected states.
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

IMPORTER_VERSION = "ahrefs_findings_machine.v1"


def _parse_crawl_date(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _parse_audit_date(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.combine(date.fromisoformat(value), datetime.min.time(), tzinfo=UTC)
    except ValueError:
        return None


def _snapshot_metric(key: str, value: Any, unit: MetricUnit) -> MetricValueResult:
    return MetricValueResult(
        metric_key=key,
        period_role=PeriodRole.snapshot,
        value_numeric=Decimal(str(value)) if value is not None else None,
        unit=unit,
        quality=Quality.complete if value is not None else Quality.partial,
    )


def parse(files: dict[str, Path], *, fallback_date: date) -> list[ParsedArtifact] | ParseSkip:
    path = files.get("ahrefs_findings_machine.json")
    if path is None:
        return ParseSkip(
            reason="ahrefs_findings_machine.json not found in run directory", level="info"
        )

    try:
        raw: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return ParseSkip(reason=f"unreadable/invalid JSON: {exc}")

    totals = raw.get("totals")
    if not isinstance(totals, dict):
        return ParseSkip(reason="unknown schema: 'totals' is not an object")
    verify_summary = raw.get("verify_summary")
    if not isinstance(verify_summary, dict):
        verify_summary = {}

    warnings: list[str] = []

    observed_at = _parse_crawl_date(raw.get("crawl_date") or "")
    if observed_at is None:
        observed_at = _parse_audit_date(raw.get("audit_date") or "")
    if observed_at is None:
        observed_at = datetime.combine(fallback_date, datetime.min.time(), tzinfo=UTC)
        warnings.append(
            "no crawl_date/audit_date in file; observed_at falls back to the run directory's date"
        )

    urls_crawled = totals.get("urls_crawled")
    if not urls_crawled:
        fallback_count = verify_summary.get("total")
        if fallback_count:
            urls_crawled = fallback_count
            warnings.append(
                "totals.urls_crawled was 0 (browser-collection path); "
                "used verify_summary.total instead"
            )

    health_score = raw.get("health_score")
    if health_score is None:
        warnings.append("health_score is null (browser-collection path has no health score)")

    metrics = [
        _snapshot_metric("ahrefs_health_score", health_score, MetricUnit.score),
        _snapshot_metric("ahrefs_errors", totals.get("errors"), MetricUnit.count),
        _snapshot_metric("ahrefs_warnings", totals.get("warnings"), MetricUnit.count),
        _snapshot_metric("ahrefs_urls_crawled", urls_crawled, MetricUnit.count),
    ]
    quality = (
        Quality.complete if all(m.value_numeric is not None for m in metrics) else Quality.partial
    )

    return [
        ParsedArtifact(
            provider=Provider.ahrefs,
            source_mode=SourceMode.auto,
            quality=quality,
            observed_at=observed_at,
            metrics=metrics,
            warnings=warnings,
            provenance={
                "domain": raw.get("domain", ""),
                "project_id": str(raw.get("project_id", "")),
            },
        )
    ]
