from __future__ import annotations

import json
from datetime import date

from app.importers.parsers import ParseSkip
from app.importers.parsers.gsc import parse


def test_family_a_skipped_without_date_dimension(tmp_path):
    path = tmp_path / "gsc_findings_machine.json"
    path.write_text(
        json.dumps(
            {
                "property_url": "https://acme.com",
                "totals": {
                    "total_clicks": 100,
                    "total_impressions": 1000,
                    "avg_position": 5.0,
                    "has_date_dimension": False,
                    "date_range_start": "",
                    "date_range_end": "",
                },
            }
        )
    )
    result = parse({"gsc_findings_machine.json": path}, fallback_date=date(2026, 6, 8))
    assert isinstance(result, ParseSkip)
    assert "has_date_dimension" in result.reason


def test_family_a_imported_when_date_dimension_present(tmp_path):
    path = tmp_path / "gsc_findings_machine.json"
    path.write_text(
        json.dumps(
            {
                "property_url": "https://acme.com",
                "totals": {
                    "total_clicks": 100,
                    "total_impressions": 1000,
                    "avg_position": 5.0,
                    "has_date_dimension": True,
                    "date_range_start": "2026-05-01",
                    "date_range_end": "2026-05-31",
                },
            }
        )
    )
    [artifact] = parse({"gsc_findings_machine.json": path}, fallback_date=date(2026, 6, 8))
    clicks = next(m for m in artifact.metrics if m.metric_key == "gsc_clicks")
    assert clicks.value_numeric == 100
    assert clicks.period_start == date(2026, 5, 1)
    assert clicks.period_end == date(2026, 5, 31)
    # CTR is never hand-set here; app.metrics.normalize derives it centrally.
    assert not any(m.metric_key == "gsc_ctr" for m in artifact.metrics)


def test_family_b_aggregates_performance_chart(tmp_path):
    path = tmp_path / "performance_chart.csv"
    path.write_text(
        "Date,Clicks,Impressions,CTR,Position\n"
        "2026-04-14,10,100,0.1,5.0\n"
        "2026-04-15,20,200,0.1,3.0\n"
    )
    [artifact] = parse({"performance_chart.csv": path}, fallback_date=date(2026, 7, 15))
    values = {m.metric_key: m for m in artifact.metrics}
    assert values["gsc_clicks"].value_numeric == 30
    assert values["gsc_impressions"].value_numeric == 300
    assert values["gsc_clicks"].period_start == date(2026, 4, 14)
    assert values["gsc_clicks"].period_end == date(2026, 4, 15)
    expected_avg_position = (5.0 * 100 + 3.0 * 200) / 300
    assert abs(float(values["gsc_avg_position"].value_numeric) - expected_avg_position) < 0.001


def test_family_b_skips_unparseable_rows(tmp_path):
    path = tmp_path / "performance_chart.csv"
    path.write_text(
        "Date,Clicks,Impressions,CTR,Position\n"
        "not-a-date,10,100,0.1,5.0\n"
        "2026-04-15,20,200,0.1,3.0\n"
    )
    [artifact] = parse({"performance_chart.csv": path}, fallback_date=date(2026, 7, 15))
    assert any("skipped 1" in w for w in artifact.warnings)
    assert next(m for m in artifact.metrics if m.metric_key == "gsc_clicks").value_numeric == 20


def test_family_b_missing_columns_is_unknown_schema(tmp_path):
    path = tmp_path / "performance_chart.csv"
    path.write_text("Foo,Bar\n1,2\n")
    result = parse({"performance_chart.csv": path}, fallback_date=date(2026, 7, 15))
    assert isinstance(result, ParseSkip)


def test_neither_file_present_is_info_skip():
    result = parse({}, fallback_date=date(2026, 1, 1))
    assert isinstance(result, ParseSkip)
    assert result.level == "info"
