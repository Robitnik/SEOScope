from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from app.importers.parsers import ParseSkip
from app.importers.parsers.ahrefs import parse


def _write(tmp_path: Path, payload: dict) -> dict[str, Path]:
    path = tmp_path / "ahrefs_findings_machine.json"
    path.write_text(json.dumps(payload))
    return {"ahrefs_findings_machine.json": path}


def test_api_path_full_data(tmp_path):
    files = _write(
        tmp_path,
        {
            "domain": "acme.com",
            "project_id": 1,
            "audit_date": "2026-06-30",
            "crawl_date": "2026-06-29T14:41:18Z",
            "health_score": 92,
            "totals": {"urls_crawled": 500, "errors": 3, "warnings": 10},
            "verify_summary": {"total": 500},
        },
    )
    [artifact] = parse(files, fallback_date=date(2026, 6, 30))
    values = {m.metric_key: m.value_numeric for m in artifact.metrics}
    assert values["ahrefs_health_score"] == 92
    assert values["ahrefs_errors"] == 3
    assert values["ahrefs_warnings"] == 10
    assert values["ahrefs_urls_crawled"] == 500
    assert artifact.observed_at.isoformat() == "2026-06-29T14:41:18+00:00"
    assert artifact.quality.value == "complete"
    assert not artifact.warnings


def test_browser_path_null_health_and_fallback_urls_crawled(tmp_path):
    files = _write(
        tmp_path,
        {
            "domain": "acme.com",
            "audit_date": "2026-07-20",
            "crawl_date": "",
            "health_score": None,
            "totals": {"urls_crawled": 0, "errors": 1, "warnings": 8},
            "verify_summary": {"total": 87},
        },
    )
    [artifact] = parse(files, fallback_date=date(2026, 7, 20))
    values = {m.metric_key: m.value_numeric for m in artifact.metrics}
    assert values["ahrefs_health_score"] is None
    assert values["ahrefs_urls_crawled"] == 87
    assert artifact.quality.value == "partial"
    assert any("browser-collection path" in w for w in artifact.warnings)
    assert any("health_score is null" in w for w in artifact.warnings)
    # audit_date (no time) still parses even though crawl_date is empty.
    assert artifact.observed_at.date() == date(2026, 7, 20)


def test_missing_file_is_info_skip():
    result = parse({}, fallback_date=date(2026, 1, 1))
    assert isinstance(result, ParseSkip)
    assert result.level == "info"


def test_unknown_schema_is_skip(tmp_path):
    path = tmp_path / "ahrefs_findings_machine.json"
    path.write_text(json.dumps({"totals": "not-an-object"}))
    result = parse({"ahrefs_findings_machine.json": path}, fallback_date=date(2026, 1, 1))
    assert isinstance(result, ParseSkip)
    assert result.level == "warning"


def test_invalid_json_is_skip(tmp_path):
    path = tmp_path / "ahrefs_findings_machine.json"
    path.write_text("{not valid json")
    result = parse({"ahrefs_findings_machine.json": path}, fallback_date=date(2026, 1, 1))
    assert isinstance(result, ParseSkip)


def test_no_dates_falls_back_to_run_directory_date(tmp_path):
    files = _write(tmp_path, {"totals": {"errors": 0, "warnings": 0, "urls_crawled": 1}})
    [artifact] = parse(files, fallback_date=date(2026, 5, 1))
    assert artifact.observed_at.date() == date(2026, 5, 1)
    assert any("falls back to the run directory" in w for w in artifact.warnings)
