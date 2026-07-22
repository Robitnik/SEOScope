from __future__ import annotations

from datetime import date
from pathlib import Path

from app.importers.scanner import scan_source


def _touch(path: Path, content: str = "{}") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def test_scans_every_recognized_kind(tmp_path):
    _touch(tmp_path / "out/acme.com_2026-06-01_ahrefs/ahrefs_findings_machine.json")
    _touch(tmp_path / "out/acme.com_2026-06-02_gsc/gsc_findings_machine.json")
    _touch(tmp_path / "out/acme.com_2026-06-03_gsc/performance_chart.csv", "Date,Clicks\n")
    _touch(tmp_path / "out/acme.com_2026-06-04_rankmove/rank_movement_machine.json")
    _touch(tmp_path / "out/semrush_acme.com_2026-06-05.json")
    _touch(tmp_path / "out/client_report_ACME_2026-06-06.xlsx", "")

    artifacts = list(scan_source(tmp_path))
    kinds = [a.kind for a in artifacts]
    assert kinds.count("ahrefs") == 1
    assert kinds.count("gsc") == 2  # Family A and Family B are two distinct runs here
    assert kinds.count("rank_movement") == 1
    assert kinds.count("semrush") == 1
    assert kinds.count("client_report") == 1

    gsc_files = {tuple(sorted(a.files)) for a in artifacts if a.kind == "gsc"}
    assert ("gsc_findings_machine.json",) in gsc_files
    assert ("performance_chart.csv",) in gsc_files


def test_ignores_cache_dirs_unrelated_pipelines_and_empty_dirs(tmp_path):
    _touch(tmp_path / "out/.rankmove_cache/foo.json")
    _touch(tmp_path / "out/acme.com_2026-06-01_framework/whatever.json")
    _touch(tmp_path / "out/acme.com_2026-06-01_blog_strategy/whatever.json")
    # recognized suffix, no files inside:
    (tmp_path / "out/acme.com_2026-06-02_gsc").mkdir(parents=True)
    (tmp_path / "out/not_a_dated_dir").mkdir(parents=True)
    (tmp_path / "out/19clv_v2_test").mkdir(parents=True)
    _touch(tmp_path / "out/client_report_ACME_2026-06-06.tsv", "x")  # tsv sibling never parsed

    assert list(scan_source(tmp_path)) == []


def test_no_out_dir_yields_nothing(tmp_path):
    assert list(scan_source(tmp_path)) == []


def test_relative_path_domain_and_run_date(tmp_path):
    _touch(tmp_path / "out/acme.com_2026-06-01_ahrefs/ahrefs_findings_machine.json")
    [artifact] = list(scan_source(tmp_path))
    assert artifact.relative_path == "out/acme.com_2026-06-01_ahrefs/ahrefs_findings_machine.json"
    assert artifact.run_date == date(2026, 6, 1)
    assert artifact.domain_or_code == "acme.com"


def test_gsc_family_a_preferred_over_family_b_when_both_present(tmp_path):
    _touch(tmp_path / "out/acme.com_2026-06-01_gsc/gsc_findings_machine.json")
    _touch(tmp_path / "out/acme.com_2026-06-01_gsc/performance_chart.csv", "Date,Clicks\n")
    [artifact] = list(scan_source(tmp_path))
    assert set(artifact.files) == {"gsc_findings_machine.json"}
