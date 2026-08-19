"""Градація серйозності редіректів за ризиком (task-51 §3.4, підтверджено

з користувачем 2026-08-19): offsite → critical, накрив живий URL/змінилась
ціль/зникло → warning, звичайне додавання → info.
"""

from __future__ import annotations

import pytest

from app.monitor.redirects.severity import (
    CRITICAL,
    INFO,
    REDIRECT_CHANGE_SEVERITY,
    WARNING,
    classify_added_rule,
    is_bulk,
    is_offsite_target,
    severity_of_redirect_change,
)


class TestIsOffsiteTarget:
    def test_relative_path_is_never_offsite(self) -> None:
        assert is_offsite_target("/new-page/", client_domain="client.com") is False

    def test_none_target_is_not_offsite(self) -> None:
        assert is_offsite_target(None, client_domain="client.com") is False

    def test_same_host_absolute_url_is_not_offsite(self) -> None:
        target = "https://client.com/new-page/"
        assert is_offsite_target(target, client_domain="client.com") is False

    def test_www_prefix_is_ignored_on_both_sides(self) -> None:
        assert is_offsite_target("https://www.client.com/x", client_domain="client.com") is False
        assert is_offsite_target("https://client.com/x", client_domain="www.client.com") is False

    def test_different_host_is_offsite(self) -> None:
        assert is_offsite_target("https://evil-spam.example/x", client_domain="client.com") is True

    def test_case_insensitive_host_comparison(self) -> None:
        assert is_offsite_target("https://CLIENT.COM/x", client_domain="client.com") is False


class TestClassifyAddedRule:
    def test_offsite_wins_over_live_url(self) -> None:
        assert classify_added_rule(offsite=True, on_live_url=True) == "redirect_offsite_added"

    def test_live_url_without_offsite(self) -> None:
        assert classify_added_rule(offsite=False, on_live_url=True) == "redirect_added_on_live_url"

    def test_plain_new_rule(self) -> None:
        assert classify_added_rule(offsite=False, on_live_url=False) == "redirect_added"


class TestSeverityTable:
    @pytest.mark.parametrize(
        ("change_type", "expected"),
        [
            ("redirect_offsite_added", CRITICAL),
            ("redirect_added_on_live_url", WARNING),
            ("redirect_target_changed", WARNING),
            ("redirect_removed", WARNING),
            ("redirects_unavailable", WARNING),
            ("redirect_added", INFO),
            ("redirect_status_changed", INFO),
            ("redirects_bulk_added", INFO),
        ],
    )
    def test_severity_of_each_change_type(self, change_type: str, expected: str) -> None:
        assert severity_of_redirect_change(change_type) == expected

    def test_every_classify_added_rule_output_has_a_severity(self) -> None:
        for offsite in (True, False):
            for on_live_url in (True, False):
                change_type = classify_added_rule(offsite=offsite, on_live_url=on_live_url)
                assert change_type in REDIRECT_CHANGE_SEVERITY


class TestIsBulk:
    def test_at_threshold_is_not_bulk(self) -> None:
        assert is_bulk(25, threshold=25) is False

    def test_above_threshold_is_bulk(self) -> None:
        assert is_bulk(26, threshold=25) is True

    def test_zero_is_not_bulk(self) -> None:
        assert is_bulk(0) is False

    def test_default_threshold_matches_plan(self) -> None:
        assert is_bulk(25) is False
        assert is_bulk(26) is True
