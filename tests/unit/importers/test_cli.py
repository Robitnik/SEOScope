from __future__ import annotations

import json

import pytest

from app.cli import main


def test_missing_source_flag_raises_system_exit():
    with pytest.raises(SystemExit):
        main(["import-ai-seo-history"])


def test_no_command_raises_system_exit():
    with pytest.raises(SystemExit):
        main([])


def test_nonexistent_source_dir_returns_error(tmp_path, capsys):
    missing = tmp_path / "does-not-exist"
    exit_code = main(["import-ai-seo-history", "--source", str(missing)])
    assert exit_code == 2
    captured = capsys.readouterr()
    assert "not a directory" in captured.err


def test_dry_run_against_empty_source(cli_env, capsys):
    source = cli_env / "aiseotools"
    (source / "out").mkdir(parents=True)

    exit_code = main(["import-ai-seo-history", "--source", str(source), "--dry-run"])
    assert exit_code == 0
    captured = capsys.readouterr()
    assert "DRY RUN" in captured.out
    assert "total artifacts scanned: 0" in captured.out


def test_real_run_reports_conflicts_before_artifacts_section(cli_env, capsys):
    source = cli_env / "aiseotools"
    (source / "out").mkdir(parents=True)
    (source / "clients.json").write_text(
        json.dumps(
            {
                "A": {"gsc": {"account_id": "sc-domain:acme.com"}},
                "B": {"gsc": {"account_id": "https://acme.com/"}},
            }
        )
    )

    exit_code = main(["import-ai-seo-history", "--source", str(source)])
    assert exit_code == 0
    captured = capsys.readouterr()
    conflicts_idx = captured.out.index("CONFLICTS")
    artifacts_idx = captured.out.index("=== artifacts ===")
    assert conflicts_idx < artifacts_idx
    assert "acme.com" in captured.out
