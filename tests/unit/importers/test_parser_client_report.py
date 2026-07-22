from __future__ import annotations

from datetime import date
from pathlib import Path

from openpyxl import Workbook

from app.importers.parsers import ParseSkip
from app.importers.parsers.client_report import parse


def _write_workbook(
    tmp_path: Path,
    historical_rows: list[tuple],
    sheet_name: str = "Historical Data",
    title_row: str | None = None,
) -> dict[str, Path]:
    wb = Workbook()
    wb.remove(wb.active)
    ws = wb.create_sheet(sheet_name)
    if title_row is not None:
        ws.append([title_row])
    ws.append(["Date", "Clicks", "Impressions", "CTR", "Organic Conversions"])
    for row in historical_rows:
        ws.append(list(row))
    path = tmp_path / "client_report.xlsx"
    wb.save(path)
    return {"client_report.xlsx": path}


def test_parses_historical_data_month_rows(tmp_path):
    files = _write_workbook(
        tmp_path,
        [
            ("May 2026", 1178, 20000, None, 50),
            ("Jun 2026", 682, 15000, None, 30),
        ],
    )
    artifacts = parse(files, fallback_date=date(2026, 6, 19))

    # `metric_values` uniques on (snapshot_id, metric_key, period_role) — one
    # snapshot can't hold two `gsc_clicks` values, so each month is its own
    # snapshot: 2 months x 2 providers = 4 artifacts, not 2.
    assert len(artifacts) == 4
    assert {a.provider.value for a in artifacts} == {"gsc", "reporting_ninja"}
    for a in artifacts:
        assert len(a.metrics) == len({m.metric_key for m in a.metrics}), (
            "one artifact must never carry two values for the same metric_key"
        )

    gsc_artifacts = [a for a in artifacts if a.provider.value == "gsc"]
    all_gsc_clicks = [m for a in gsc_artifacts for m in a.metrics if m.metric_key == "gsc_clicks"]
    assert {m.value_numeric for m in all_gsc_clicks} == {1178, 682}
    june_clicks = next(m for m in all_gsc_clicks if m.period_start == date(2026, 6, 1))
    assert june_clicks.period_end == date(2026, 6, 30)
    # CTR is never hand-set here; app.metrics.normalize derives it centrally.
    assert not any(m.metric_key == "gsc_ctr" for a in gsc_artifacts for m in a.metrics)

    rn_artifacts = [a for a in artifacts if a.provider.value == "reporting_ninja"]
    rn_conversions = [
        m for a in rn_artifacts for m in a.metrics if m.metric_key == "rn_organic_conversions"
    ]
    assert {m.value_numeric for m in rn_conversions} == {50, 30}
    assert any("This Month" in w for a in artifacts for w in a.warnings)


def test_title_row_before_header_is_tolerated(tmp_path):
    """Real samples (`client_report_PJC_2026-06-19.xlsx`) put a title row
    ("PJC — Monthly History") before the actual column header.
    """
    files = _write_workbook(
        tmp_path,
        [("Jun 2026", 682, 15000, None, 30)],
        title_row="PJC — Monthly History",
    )
    [gsc_artifact, _rn] = parse(files, fallback_date=date(2026, 6, 19))
    clicks = next(m for m in gsc_artifact.metrics if m.metric_key == "gsc_clicks")
    assert clicks.value_numeric == 682


def test_empty_historical_sheet_is_info_skip(tmp_path):
    files = _write_workbook(tmp_path, [])
    result = parse(files, fallback_date=date(2026, 6, 19))
    assert isinstance(result, ParseSkip)
    assert result.level == "info"


def test_missing_historical_sheet_is_unknown_schema(tmp_path):
    files = _write_workbook(
        tmp_path, [("Jun 2026", 1, 2, None, 3)], sheet_name="Current Month Data"
    )
    result = parse(files, fallback_date=date(2026, 6, 19))
    assert isinstance(result, ParseSkip)
    assert "Historical Data" in result.reason


def test_missing_file_is_info_skip():
    result = parse({}, fallback_date=date(2026, 1, 1))
    assert isinstance(result, ParseSkip)
    assert result.level == "info"


def test_unparseable_month_label_is_skipped_row_not_whole_file(tmp_path):
    files = _write_workbook(
        tmp_path,
        [
            ("not a month", 1, 2, None, 3),
            ("Jun 2026", 682, 15000, None, 30),
        ],
    )
    [gsc_artifact, _rn] = parse(files, fallback_date=date(2026, 6, 19))
    assert any("unparseable month label" in w for w in gsc_artifact.warnings)
    clicks = [m for m in gsc_artifact.metrics if m.metric_key == "gsc_clicks"]
    assert {m.value_numeric for m in clicks} == {682}
