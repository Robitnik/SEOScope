"""Diff engine (task-35 §3.4): turns an old state + a new observation into a
list of `DetectedChange`. Pure — no DB access, no HTTP. `app/monitor/runner.py`
is the only caller, and is the one that persists versions/changes and decides
self-healing (comparing against what is still open).

The very first run for a client alerts on nothing (task-35 §3.4 "Перший
прогін клієнта нічого не алертить") — every function here takes
`is_baseline_run` and short-circuits to `[]` when it is set, regardless of
what changed, so a brand-new client's first 2,850 "new page" facts never hit
the change feed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from app.monitor.severity import severity_of


@dataclass(frozen=True)
class DetectedChange:
    change_type: str
    severity: str
    scope: str  # "site" | "page" | "dns" | "robots" | "sitemap"
    subject: str
    old_value: str | None = None
    new_value: str | None = None
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PageObservation:
    status: int | None
    final_url: str | None
    meta_robots: str | None
    x_robots_tag: str | None
    canonical: str | None
    title: str | None
    description: str | None
    h1: str | None
    indexable: bool | None
    analytics_ids: frozenset[str]
    google_site_verifications: frozenset[str]
    hop_count: int | None


def _has_noindex(meta_robots: str | None, x_robots_tag: str | None) -> bool:
    combined = f"{meta_robots or ''} {x_robots_tag or ''}".lower()
    return "noindex" in combined


def _host(url: str | None) -> str | None:
    if not url:
        return None
    return (urlsplit(url).hostname or "").lower()


def diff_page(
    *,
    url: str,
    client_domain: str,
    old: PageObservation | None,
    new: PageObservation,
    is_key_page: bool,
    is_baseline_run: bool,
) -> list[DetectedChange]:
    if is_baseline_run:
        return []
    changes: list[DetectedChange] = []

    if old is None:
        changes.append(DetectedChange("page_new", severity_of("page_new"), "page", url))
        return changes

    old_noindex = _has_noindex(old.meta_robots, old.x_robots_tag)
    new_noindex = _has_noindex(new.meta_robots, new.x_robots_tag)
    if new_noindex and not old_noindex:
        changes.append(
            DetectedChange(
                "page_noindex_appeared",
                severity_of("page_noindex_appeared"),
                "page",
                url,
                old_value=old.meta_robots,
                new_value=new.meta_robots or new.x_robots_tag,
            )
        )

    if old.canonical != new.canonical:
        new_host = _host(new.canonical)
        client_host = client_domain.lower().removeprefix("www.")
        is_offsite = (
            bool(new.canonical)
            and new_host is not None
            and new_host.removeprefix("www.") != client_host
        )
        change_type = "canonical_offsite" if is_offsite else "canonical_changed"
        changes.append(
            DetectedChange(
                change_type,
                severity_of(change_type),
                "page",
                url,
                old_value=old.canonical,
                new_value=new.canonical,
            )
        )

    for field_name, change_type in (
        ("title", "title_changed"),
        ("description", "description_changed"),
        ("h1", "h1_changed"),
    ):
        old_value = getattr(old, field_name)
        new_value = getattr(new, field_name)
        if old_value != new_value and (old_value or new_value):
            changes.append(
                DetectedChange(
                    change_type,
                    severity_of(change_type),
                    "page",
                    url,
                    old_value=old_value,
                    new_value=new_value,
                )
            )

    if is_key_page:
        lost_verifications = old.google_site_verifications - new.google_site_verifications
        if lost_verifications and old.google_site_verifications:
            changes.append(
                DetectedChange(
                    "gsc_verification_lost",
                    severity_of("gsc_verification_lost"),
                    "site",
                    url,
                    old_value=", ".join(sorted(lost_verifications)),
                    details={"lost_tokens": sorted(lost_verifications)},
                )
            )

        lost_analytics = old.analytics_ids - new.analytics_ids
        if lost_analytics and old.analytics_ids:
            changes.append(
                DetectedChange(
                    "analytics_tag_lost",
                    severity_of("analytics_tag_lost"),
                    "site",
                    url,
                    old_value=", ".join(sorted(lost_analytics)),
                    details={"lost_ids": sorted(lost_analytics)},
                )
            )
        added_analytics = new.analytics_ids - old.analytics_ids
        if added_analytics:
            changes.append(
                DetectedChange(
                    "analytics_tag_added",
                    severity_of("analytics_tag_added"),
                    "site",
                    url,
                    new_value=", ".join(sorted(added_analytics)),
                    details={"added_ids": sorted(added_analytics)},
                )
            )

    old_had_redirect = (old.hop_count or 0) > 0
    new_has_redirect = (new.hop_count or 0) > 0
    if new_has_redirect and not old_had_redirect and new.status and 200 <= new.status < 400:
        changes.append(
            DetectedChange(
                "page_redirect_appeared",
                severity_of("page_redirect_appeared"),
                "page",
                url,
                new_value=new.final_url,
            )
        )

    old_ok = old.status is not None and old.status < 400
    new_broken = new.status is None or new.status >= 400
    if old_ok and new_broken:
        changes.append(
            DetectedChange(
                "page_status_error",
                severity_of("page_status_error"),
                "page",
                url,
                old_value=str(old.status),
                new_value=str(new.status) if new.status is not None else "unreachable",
            )
        )

    return changes


def diff_content(
    *,
    url: str,
    old_text_sha256: str | None,
    new_text_sha256: str | None,
    old_raw_sha256: str | None,
    new_raw_sha256: str | None,
    is_baseline_run: bool,
) -> list[DetectedChange]:
    """`info`-level "something moved" facts, independent of the SEO-signal
    diff above — a page can reword its copy without any signal changing.
    """
    if is_baseline_run or old_text_sha256 is None:
        return []
    if new_text_sha256 != old_text_sha256:
        return [DetectedChange("content_changed", severity_of("content_changed"), "page", url)]
    if new_raw_sha256 != old_raw_sha256:
        return [DetectedChange("page_body_changed", severity_of("page_body_changed"), "page", url)]
    return []


def diff_robots(
    *,
    domain: str,
    old_status: int | None,
    new_status: int | None,
    old_blocking: list[str],
    new_blocking: list[str],
    changed_group_fields: list[str],
    is_baseline_run: bool,
) -> list[DetectedChange]:
    if is_baseline_run:
        return []
    changes: list[DetectedChange] = []
    subject = f"{domain}/robots.txt"

    if new_status is not None and new_status >= 500:
        changes.append(
            DetectedChange(
                "robots_unavailable", severity_of("robots_unavailable"), "robots", subject
            )
        )
    elif new_status == 404:
        changes.append(
            DetectedChange("robots_missing", severity_of("robots_missing"), "robots", subject)
        )

    if new_blocking and not old_blocking:
        changes.append(
            DetectedChange(
                "robots_blocks_site",
                severity_of("robots_blocks_site"),
                "robots",
                subject,
                new_value=", ".join(new_blocking),
                details={"blocked_user_agents": new_blocking},
            )
        )
    elif changed_group_fields and new_status is not None and new_status < 400:
        changes.append(
            DetectedChange(
                "robots_rules_changed",
                severity_of("robots_rules_changed"),
                "robots",
                subject,
                details={"changed_groups": changed_group_fields},
            )
        )

    return changes


def diff_sitemap_resource(
    *, resource_url: str, new_status: int | None, is_baseline_run: bool
) -> list[DetectedChange]:
    """Reported every run the sitemap stays gone, not just the run it broke
    on — `MonitorRepository.open_change` dedups that into one open row.
    """
    if is_baseline_run:
        return []
    if new_status is None or new_status == 404:
        return [
            DetectedChange("sitemap_lost", severity_of("sitemap_lost"), "sitemap", resource_url)
        ]
    return []


def diff_sitemap_url_removals(
    *,
    resource_url: str,
    removed: set[str],
    total_before: int,
    drop_alert_pct: float,
    is_baseline_run: bool,
) -> list[DetectedChange]:
    if is_baseline_run or not removed or total_before <= 0:
        return []
    pct = (len(removed) / total_before) * 100
    if pct < drop_alert_pct and len(removed) < 10:
        return []
    examples = sorted(removed)[:10]
    return [
        DetectedChange(
            "sitemap_urls_removed",
            severity_of("sitemap_urls_removed"),
            "sitemap",
            resource_url,
            details={"removed_count": len(removed), "examples": examples},
        )
    ]


def diff_dns(
    *,
    client_domain: str,
    name: str,
    record_type: str,
    old_values: frozenset[str],
    new_values: frozenset[str],
    is_baseline_run: bool,
) -> list[DetectedChange]:
    if is_baseline_run or old_values == new_values:
        return []
    is_apex = name.lower().removeprefix("www.") == client_domain.lower().removeprefix("www.")
    changes: list[DetectedChange] = []

    if record_type == "NS" and old_values:
        changes.append(
            DetectedChange(
                "dns_ns_changed",
                severity_of("dns_ns_changed"),
                "dns",
                name,
                old_value=", ".join(sorted(old_values)),
                new_value=", ".join(sorted(new_values)),
            )
        )
    elif record_type in ("A", "AAAA") and is_apex:
        changes.append(
            DetectedChange(
                "dns_apex_a_changed",
                severity_of("dns_apex_a_changed"),
                "dns",
                name,
                old_value=", ".join(sorted(old_values)),
                new_value=", ".join(sorted(new_values)),
            )
        )
    elif record_type == "TXT":
        from app.monitor.dns import extract_google_site_verification_tokens

        old_tokens = set(extract_google_site_verification_tokens(tuple(old_values)))
        new_tokens = set(extract_google_site_verification_tokens(tuple(new_values)))
        lost_tokens = old_tokens - new_tokens
        if lost_tokens and old_tokens:
            changes.append(
                DetectedChange(
                    "gsc_verification_lost",
                    severity_of("gsc_verification_lost"),
                    "dns",
                    name,
                    old_value=", ".join(sorted(lost_tokens)),
                    details={"lost_tokens": sorted(lost_tokens)},
                )
            )
        else:
            changes.append(
                DetectedChange("dns_txt_changed", severity_of("dns_txt_changed"), "dns", name)
            )
    elif record_type == "MX":
        changes.append(DetectedChange("dns_mx_changed", severity_of("dns_mx_changed"), "dns", name))

    return changes
