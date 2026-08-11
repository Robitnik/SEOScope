"""Severity table (task-35 §3.4/§5) — the full table, self-healing, and the
sitemap "URL != 200" aggregate rules (§3.4.1)."""

from __future__ import annotations

import pytest

from app.monitor.severity import (
    CRITICAL,
    INFO,
    STATIC_SEVERITY,
    WARNING,
    classify_sitemap_status,
    resolved_change_type,
    severity_of,
    sitemap_broken_summary_severity,
    sitemap_url_status_change,
    tls_change,
)


@pytest.mark.parametrize(
    "change_type",
    [
        "page_noindex_appeared",
        "robots_blocks_site",
        "robots_unavailable",
        "gsc_verification_lost",
        "analytics_tag_lost",
        "site_down",
        "dns_ns_changed",
        "dns_apex_a_changed",
        "canonical_offsite",
        "sitemap_lost",
    ],
)
def test_critical_change_types(change_type):
    assert severity_of(change_type) == CRITICAL


@pytest.mark.parametrize(
    "change_type",
    [
        "canonical_changed",
        "title_changed",
        "description_changed",
        "h1_changed",
        "page_status_error",
        "page_redirect_appeared",
        "page_gone",
        "sitemap_urls_removed",
        "robots_rules_changed",
        "dns_txt_changed",
        "dns_mx_changed",
        "monitor_blocked",
        "dns_unavailable",
    ],
)
def test_warning_change_types(change_type):
    assert severity_of(change_type) == WARNING


@pytest.mark.parametrize(
    "change_type",
    [
        "page_new",
        "content_changed",
        "sitemap_lastmod_changed",
        "analytics_tag_added",
        "robots_missing",
        "page_body_changed",
    ],
)
def test_info_change_types(change_type):
    assert severity_of(change_type) == INFO


def test_every_static_severity_value_is_one_of_the_three_levels():
    assert set(STATIC_SEVERITY.values()) <= {CRITICAL, WARNING, INFO}


def test_resolved_change_type_is_always_info_and_suffixed():
    assert resolved_change_type("page_noindex_appeared") == "page_noindex_appeared_resolved"


@pytest.mark.parametrize(
    ("days_left", "expected"),
    [
        (-1, ("tls_expired", CRITICAL)),
        (0, ("tls_expired", CRITICAL)),
        (3, ("tls_expiring_3d", CRITICAL)),
        (14, ("tls_expiring_14d", WARNING)),
        (15, None),
        (None, None),
    ],
)
def test_tls_change_thresholds(days_left, expected):
    assert tls_change(days_left) == expected


def test_sitemap_url_status_3xx_is_always_warning():
    assert sitemap_url_status_change("3xx", is_key_page=True, consecutive_failures=5) == (
        "sitemap_url_redirects",
        WARNING,
    )


def test_sitemap_url_status_4xx_escalates_on_key_page():
    assert sitemap_url_status_change("4xx", is_key_page=False, consecutive_failures=0)[1] == WARNING
    assert sitemap_url_status_change("4xx", is_key_page=True, consecutive_failures=0)[1] == CRITICAL


def test_sitemap_url_status_5xx_escalates_after_second_consecutive_failure():
    assert sitemap_url_status_change("5xx", is_key_page=False, consecutive_failures=1)[1] == WARNING
    assert (
        sitemap_url_status_change("5xx", is_key_page=False, consecutive_failures=2)[1] == CRITICAL
    )


def test_sitemap_broken_summary_matches_the_live_exxxoticaexpo_measurement():
    # Live regression (task-35 §1.1/§3.4.1): 2/40 sampled URLs were 301s to a
    # different page — must land as `warning`, not silence and not `critical`.
    assert (
        sitemap_broken_summary_severity(
            2, 40, warn_pct=2, warn_count=10, alert_pct=10, alert_count=50
        )
        == WARNING
    )


def test_sitemap_broken_summary_clean_portfolio_is_none():
    assert (
        sitemap_broken_summary_severity(
            0, 40, warn_pct=2, warn_count=10, alert_pct=10, alert_count=50
        )
        is None
    )


def test_sitemap_broken_summary_escalates_to_critical_past_alert_threshold():
    assert (
        sitemap_broken_summary_severity(
            30, 100, warn_pct=2, warn_count=10, alert_pct=10, alert_count=50
        )
        == CRITICAL
    )


def test_sitemap_broken_summary_count_threshold_also_triggers():
    # 1% but 60 URLs — over the absolute alert_count even though under alert_pct.
    assert (
        sitemap_broken_summary_severity(
            60, 6000, warn_pct=2, warn_count=10, alert_pct=10, alert_count=50
        )
        == CRITICAL
    )


@pytest.mark.parametrize(
    ("status", "expected"),
    [(200, "ok"), (301, "redirect"), (404, "broken"), (500, "unavailable"), (None, "unavailable")],
)
def test_classify_sitemap_status(status, expected):
    assert classify_sitemap_status(status) == expected
