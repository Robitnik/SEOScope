"""GSC parser (task-07): two incompatible real formats share the `_gsc`
directory suffix, detected by which files are present — there is no schema
version field in either.

Family A (`gsc_findings_machine.json`, the `gsc-import` pipeline's ingested
output) carries `totals.total_clicks` etc., but in every real sample found
`totals.has_date_dimension` was `false` and `date_range_start`/`end` were
empty — the underlying CSV export was never run with GSC's "Date" grouping
enabled. A range metric needs an exact period; inventing one from the run
directory's date would misrepresent what's often a multi-month aggregate as
a one-day snapshot, so Family A is skipped whenever no exact range is
present in the file (checked, not assumed, in case a future export does
carry one).

Family B (`performance_chart.csv`, a browser-scrape export that was never
run through the `gsc-import` ingest step) has genuine daily
`Date,Clicks,Impressions,CTR,Position` rows — the exact date range comes
from this file's own `Date` column, no fallback needed. `CTR` is never
hand-set from either family: derived CTR is computed once, centrally, by
`app.metrics.normalize.normalize_result` from clicks/impressions.
"""

from __future__ import annotations

import csv
import json
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from app.importers.parsers import ParsedArtifact, ParseSkip
from app.schemas.adapter import MetricValueResult
from app.schemas.enums import MetricUnit, PeriodRole, Provider, Quality, SourceMode

IMPORTER_VERSION = "gsc.v1"

_DATE_FORMATS = ("%Y-%m-%d", "%b %d, %Y", "%B %d, %Y", "%m/%d/%Y")


def _parse_date(value: str) -> date | None:
    value = (value or "").strip()
    if not value:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None


def _range_metric(
    key: str, value: Any, start: date, end: date, unit: MetricUnit
) -> MetricValueResult:
    return MetricValueResult(
        metric_key=key,
        period_role=PeriodRole.current,
        period_start=start,
        period_end=end,
        value_numeric=Decimal(str(value)) if value is not None else None,
        unit=unit,
        quality=Quality.complete if value is not None else Quality.partial,
    )


def parse(files: dict[str, Path], *, fallback_date: date) -> list[ParsedArtifact] | ParseSkip:
    if "gsc_findings_machine.json" in files:
        return _parse_family_a(files["gsc_findings_machine.json"])
    if "performance_chart.csv" in files:
        return _parse_family_b(files["performance_chart.csv"])
    return ParseSkip(
        reason="no known GSC artifact (gsc_findings_machine.json / performance_chart.csv) found",
        level="info",
    )


def _parse_family_a(path: Path) -> list[ParsedArtifact] | ParseSkip:
    try:
        raw: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return ParseSkip(reason=f"unreadable/invalid JSON: {exc}")

    totals = raw.get("totals")
    if not isinstance(totals, dict):
        return ParseSkip(reason="unknown schema: 'totals' is not an object")

    if not totals.get("has_date_dimension"):
        return ParseSkip(
            reason="totals.has_date_dimension is false (no exact date range); "
            "refusing to guess a GSC period"
        )
    start = _parse_date(totals.get("date_range_start", ""))
    end = _parse_date(totals.get("date_range_end", ""))
    if start is None or end is None:
        return ParseSkip(
            reason="has_date_dimension is true but date_range_start/end did not parse; "
            "refusing to guess a GSC period"
        )

    metrics = [
        _range_metric("gsc_clicks", totals.get("total_clicks"), start, end, MetricUnit.count),
        _range_metric(
            "gsc_impressions", totals.get("total_impressions"), start, end, MetricUnit.count
        ),
        _range_metric(
            "gsc_avg_position", totals.get("avg_position"), start, end, MetricUnit.position
        ),
    ]
    quality = (
        Quality.complete if all(m.value_numeric is not None for m in metrics) else Quality.partial
    )
    observed_at = datetime.combine(end, datetime.min.time(), tzinfo=UTC)

    return [
        ParsedArtifact(
            provider=Provider.gsc,
            source_mode=SourceMode.auto,
            quality=quality,
            observed_at=observed_at,
            data_through=end,
            metrics=metrics,
            provenance={"property_url": raw.get("property_url", "")},
        )
    ]


def _parse_family_b(path: Path) -> list[ParsedArtifact] | ParseSkip:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        return ParseSkip(reason=f"unreadable file: {exc}")

    reader = csv.DictReader(text.splitlines())
    if reader.fieldnames is None:
        return ParseSkip(reason="empty performance_chart.csv")
    fields = {(name or "").strip().lower(): name for name in reader.fieldnames}
    required = {"date", "clicks", "impressions", "position"}
    missing = required - set(fields)
    if missing:
        return ParseSkip(
            reason=f"unknown schema: performance_chart.csv missing columns {sorted(missing)}"
        )

    total_clicks = 0
    total_impressions = 0
    weighted_position_sum = Decimal(0)
    dates: list[date] = []
    skipped_rows = 0

    for row in reader:
        d = _parse_date(row[fields["date"]] or "")
        if d is None:
            skipped_rows += 1
            continue
        try:
            clicks = int(float(row[fields["clicks"]] or 0))
            impressions = int(float(row[fields["impressions"]] or 0))
            position = Decimal(row[fields["position"]] or "0")
        except (ValueError, InvalidOperation):
            skipped_rows += 1
            continue
        dates.append(d)
        total_clicks += clicks
        total_impressions += impressions
        weighted_position_sum += position * impressions

    if not dates:
        return ParseSkip(reason="no parseable rows in performance_chart.csv")

    start, end = min(dates), max(dates)
    avg_position = (weighted_position_sum / total_impressions) if total_impressions else None

    metrics = [
        _range_metric("gsc_clicks", total_clicks, start, end, MetricUnit.count),
        _range_metric("gsc_impressions", total_impressions, start, end, MetricUnit.count),
        _range_metric("gsc_avg_position", avg_position, start, end, MetricUnit.position),
    ]
    warnings = (
        [f"skipped {skipped_rows} unparseable row(s) in performance_chart.csv"]
        if skipped_rows
        else []
    )
    quality = Quality.complete if avg_position is not None else Quality.partial
    observed_at = datetime.combine(end, datetime.min.time(), tzinfo=UTC)

    return [
        ParsedArtifact(
            provider=Provider.gsc,
            source_mode=SourceMode.browser,
            quality=quality,
            observed_at=observed_at,
            data_through=end,
            metrics=metrics,
            warnings=warnings,
        )
    ]
