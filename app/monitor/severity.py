"""Severity table (task-35 §3.4/§3.4.1) — the single source of truth every
other module reads instead of hardcoding a level next to a change.

Most change types have a fixed severity (`STATIC_SEVERITY`). A handful are
context-dependent and get their own function: TLS days-left, and the sitemap
"URL != 200" family, whose severity depends on whether the URL is a key page
and how many consecutive failures it has racked up (task-35 §3.4.1).
"""

from __future__ import annotations

CRITICAL = "critical"
WARNING = "warning"
INFO = "info"

SEVERITIES = (CRITICAL, WARNING, INFO)

# "Sайт or its measurability is broken right now" (task-35 §3.4).
STATIC_SEVERITY: dict[str, str] = {
    # critical
    "page_noindex_appeared": CRITICAL,
    "robots_blocks_site": CRITICAL,
    "robots_unavailable": CRITICAL,
    "gsc_verification_lost": CRITICAL,
    "analytics_tag_lost": CRITICAL,
    "site_down": CRITICAL,
    "dns_ns_changed": CRITICAL,
    "dns_apex_a_changed": CRITICAL,
    "canonical_offsite": CRITICAL,
    "sitemap_lost": CRITICAL,
    # warning
    "canonical_changed": WARNING,
    "title_changed": WARNING,
    "description_changed": WARNING,
    "h1_changed": WARNING,
    "page_status_error": WARNING,
    "page_redirect_appeared": WARNING,
    "page_gone": WARNING,
    "sitemap_urls_removed": WARNING,
    "robots_rules_changed": WARNING,
    "dns_txt_changed": WARNING,
    "dns_mx_changed": WARNING,
    "monitor_blocked": WARNING,
    # Both configured DoH resolvers failed to answer — a fetch problem on our
    # side, never treated as "the records are gone" (PLAN §12, risk table).
    "dns_unavailable": WARNING,
    # info
    "page_new": INFO,
    "content_changed": INFO,
    "sitemap_lastmod_changed": INFO,
    "analytics_tag_added": INFO,
    "robots_missing": INFO,
    "page_body_changed": INFO,
}


def severity_of(change_type: str) -> str:
    return STATIC_SEVERITY[change_type]


def resolved_change_type(change_type: str) -> str:
    """Self-healing event (task-35 §3.4 "Самозагасання") — always `info`."""
    return f"{change_type}_resolved"


def tls_change(days_left: int | None) -> tuple[str, str] | None:
    """`(change_type, severity)` for the current TLS state, or `None` if the
    certificate is not close enough to expiry to say anything.
    """
    if days_left is None:
        return None
    if days_left <= 0:
        return "tls_expired", CRITICAL
    if days_left <= 3:
        return "tls_expiring_3d", CRITICAL
    if days_left <= 14:
        return "tls_expiring_14d", WARNING
    return None


def sitemap_url_status_change(
    status_kind: str, *, is_key_page: bool, consecutive_failures: int
) -> tuple[str, str]:
    """`(change_type, severity)` for one sitemap URL whose final status != 200
    (task-35 §3.4.1). `status_kind` is `"3xx" | "4xx" | "5xx"`.
    """
    if status_kind == "3xx":
        return "sitemap_url_redirects", WARNING
    if status_kind == "4xx":
        return "sitemap_url_broken", (CRITICAL if is_key_page else WARNING)
    # 5xx / timeout: warning once, critical from the second consecutive miss.
    escalate = is_key_page or consecutive_failures >= 2
    return "sitemap_url_unavailable", (CRITICAL if escalate else WARNING)


def classify_sitemap_status(status: int | None) -> str:
    """`"ok" | "redirect" | "broken" | "unavailable"` — shared by the runner's
    aggregate-severity calculation and the API's `sitemap_health` counts
    (task-35 §3.4.1), so the two can never disagree about what counts as
    broken.
    """
    if status is None or status >= 500:
        return "unavailable"
    if status >= 400:
        return "broken"
    if status >= 300:
        return "redirect"
    return "ok"


def sitemap_broken_summary_severity(
    broken_count: int,
    total_count: int,
    *,
    warn_pct: float,
    warn_count: int,
    alert_pct: float,
    alert_count: int,
) -> str | None:
    """Aggregate `sitemap_broken_urls` level (task-35 §3.4.1) — `None` means
    the portfolio is clean enough not to say anything.
    """
    if total_count <= 0 or broken_count <= 0:
        return None
    pct = (broken_count / total_count) * 100
    if pct >= alert_pct or broken_count >= alert_count:
        return CRITICAL
    if pct >= warn_pct or broken_count >= warn_count:
        return WARNING
    return None
