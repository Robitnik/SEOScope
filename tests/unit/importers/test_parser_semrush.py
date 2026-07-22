from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from app.importers.parsers import ParseSkip
from app.importers.parsers.semrush import parse


def _write(tmp_path: Path, payload: dict) -> dict[str, Path]:
    path = tmp_path / "semrush_snapshot.json"
    path.write_text(json.dumps(payload))
    return {"semrush_snapshot.json": path}


def test_full_overview_present(tmp_path):
    files = _write(
        tmp_path,
        {
            "domain": "acme.com",
            "database": "us",
            "date": "2026-07-14",
            "overview": {"authority_score": 45, "organic_traffic": 1000, "organic_keywords": 200},
            "backlinks_overview": {"domains_num": 352, "ascore": None},
            "warnings": [],
        },
    )
    [artifact] = parse(files, fallback_date=date(2026, 7, 14))
    values = {m.metric_key: m.value_numeric for m in artifact.metrics}
    assert values["semrush_authority_score"] == 45
    assert values["semrush_organic_traffic"] == 1000
    assert values["semrush_referring_domains"] == 352
    assert artifact.quality.value == "complete"
    assert artifact.observed_at.date() == date(2026, 7, 14)


def test_overview_null_falls_back_to_backlinks_ascore(tmp_path):
    files = _write(
        tmp_path,
        {
            "domain": "acme.com",
            "database": "us",
            "date": "2026-07-14",
            "overview": None,
            "backlinks_overview": {"domains_num": 352, "ascore": 30},
            "warnings": ["sr_overview.csv not found — overview metrics unavailable."],
        },
    )
    [artifact] = parse(files, fallback_date=date(2026, 7, 14))
    values = {m.metric_key: m.value_numeric for m in artifact.metrics}
    assert values["semrush_authority_score"] == 30
    assert values["semrush_organic_traffic"] is None
    assert "sr_overview.csv not found — overview metrics unavailable." in artifact.warnings


def test_everything_null_still_emits_referring_domains(tmp_path):
    files = _write(
        tmp_path,
        {
            "domain": "acme.com",
            "date": "2026-07-14",
            "overview": None,
            "backlinks_overview": {"domains_num": 352, "ascore": None},
        },
    )
    [artifact] = parse(files, fallback_date=date(2026, 7, 14))
    values = {m.metric_key: m.value_numeric for m in artifact.metrics}
    assert values["semrush_authority_score"] is None
    assert values["semrush_referring_domains"] == 352
    assert artifact.quality.value == "partial"
    assert any("authority_score unavailable" in w for w in artifact.warnings)


def test_missing_both_overview_keys_is_unknown_schema(tmp_path):
    path = tmp_path / "semrush_snapshot.json"
    path.write_text(json.dumps({"domain": "acme.com"}))
    result = parse({"semrush_snapshot.json": path}, fallback_date=date(2026, 1, 1))
    assert isinstance(result, ParseSkip)


def test_missing_file_is_info_skip():
    result = parse({}, fallback_date=date(2026, 1, 1))
    assert isinstance(result, ParseSkip)
    assert result.level == "info"


def test_missing_date_falls_back_to_run_directory_date(tmp_path):
    files = _write(
        tmp_path,
        {"domain": "acme.com", "overview": {"authority_score": 1}, "backlinks_overview": {}},
    )
    [artifact] = parse(files, fallback_date=date(2026, 3, 3))
    assert artifact.observed_at.date() == date(2026, 3, 3)
    assert any("falls back to the run directory" in w for w in artifact.warnings)
