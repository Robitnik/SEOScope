"""One shared account snapshot instead of a cabinet session per question."""

from __future__ import annotations

import json
import time

import pytest

from app.reports import ninja_browser, ninja_catalog


@pytest.fixture(autouse=True)
def _isolated_cabinet(monkeypatch, tmp_path):
    monkeypatch.setenv("REPORTING_NINJA_NAMES_CACHE_PATH", str(tmp_path / "names.json"))
    monkeypatch.setenv("REPORTING_NINJA_EMAIL", "ops@example.com")
    monkeypatch.setenv("REPORTING_NINJA_PASSWORD", "secret")
    monkeypatch.delenv("REPORTING_NINJA_NAMES_TTL_SECONDS", raising=False)


def _fake_cabinet(monkeypatch, names, *, truncated=False, error=None):
    passes: list[str] = []

    def fake_list_report_names(**_kwargs):
        passes.append("pass")
        if error is not None:
            raise RuntimeError(error)
        return list(names), truncated

    monkeypatch.setattr(ninja_browser, "list_report_names", fake_list_report_names)
    return passes


ACCOUNT = ["SDS-client", "ADA-client", "SDS-Internal report"]


def test_the_first_read_pays_for_one_cabinet_pass_and_the_next_ones_do_not(monkeypatch):
    passes = _fake_cabinet(monkeypatch, ACCOUNT)

    first = ninja_catalog.get_account_reports()
    second = ninja_catalog.get_account_reports()

    assert first.names == sorted(set(ACCOUNT))
    assert second.names == first.names
    assert passes == ["pass"]


def test_force_refresh_re_reads_the_cabinet(monkeypatch):
    passes = _fake_cabinet(monkeypatch, ACCOUNT)
    ninja_catalog.get_account_reports()
    ninja_catalog.get_account_reports(force_refresh=True)
    assert passes == ["pass", "pass"]


def test_a_snapshot_past_its_ttl_is_re_read(monkeypatch):
    monkeypatch.setenv("REPORTING_NINJA_NAMES_TTL_SECONDS", "0")
    passes = _fake_cabinet(monkeypatch, ACCOUNT)
    ninja_catalog.get_account_reports()
    time.sleep(0.01)
    ninja_catalog.get_account_reports()
    assert passes == ["pass", "pass"]


def test_the_snapshot_survives_a_restart_because_it_is_on_disk(monkeypatch, tmp_path):
    # The api and worker are separate processes and an api restart must not
    # cost the operator another cabinet pass.
    _fake_cabinet(monkeypatch, ACCOUNT)
    ninja_catalog.get_account_reports()

    payload = json.loads((tmp_path / "names.json").read_text())
    assert sorted(payload["names"]) == sorted(set(ACCOUNT))

    passes = _fake_cabinet(monkeypatch, ["something-else"])
    assert ninja_catalog.get_account_reports().names == sorted(set(ACCOUNT))
    assert passes == []


def test_a_corrupt_cache_file_is_ignored_not_read_as_a_short_account(monkeypatch, tmp_path):
    (tmp_path / "names.json").write_text("{ this is not json")
    passes = _fake_cabinet(monkeypatch, ACCOUNT)
    assert ninja_catalog.get_account_reports().names == sorted(set(ACCOUNT))
    assert passes == ["pass"]


# --- what may and may not count as proof a report does not exist -------------


def test_a_complete_snapshot_can_prove_a_name_absent(monkeypatch):
    _fake_cabinet(monkeypatch, ACCOUNT)
    snapshot = ninja_catalog.get_account_reports()
    assert snapshot.has("SDS-client")
    assert snapshot.proves_absent("SDS-typo report")


def test_a_truncated_snapshot_proves_nothing_absent(monkeypatch):
    # task-48 §1.7: the scroll ran out of budget, so a name's absence from
    # the list is an unanswered question, not a fact.
    _fake_cabinet(monkeypatch, ACCOUNT, truncated=True)
    snapshot = ninja_catalog.get_account_reports()
    assert snapshot.truncated is True
    assert not snapshot.proves_absent("SWM-client")


def test_a_failed_cabinet_pass_proves_nothing_absent(monkeypatch):
    _fake_cabinet(monkeypatch, [], error="the reports cabinet showed the login form")
    snapshot = ninja_catalog.get_account_reports()
    assert snapshot.error is not None
    assert not snapshot.proves_absent("SDS-client")


def test_without_credentials_the_question_cannot_be_asked_at_all(monkeypatch):
    monkeypatch.delenv("REPORTING_NINJA_EMAIL", raising=False)
    monkeypatch.delenv("REPORTING_NINJA_PASSWORD", raising=False)
    passes = _fake_cabinet(monkeypatch, ACCOUNT)

    snapshot = ninja_catalog.get_account_reports()
    assert snapshot.available is False
    assert not snapshot.proves_absent("SDS-client")
    assert passes == []  # no browser is launched to find that out


def test_a_failed_refresh_does_not_discard_the_last_good_snapshot(monkeypatch):
    _fake_cabinet(monkeypatch, ACCOUNT)
    ninja_catalog.get_account_reports()

    _fake_cabinet(monkeypatch, [], error="cabinet busy")
    snapshot = ninja_catalog.get_account_reports(force_refresh=True)
    assert snapshot.names == sorted(set(ACCOUNT))
    assert snapshot.error is not None  # served, but honestly labelled
    assert not snapshot.proves_absent("SWM-client")
