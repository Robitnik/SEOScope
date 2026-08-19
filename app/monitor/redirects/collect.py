"""Оркестрація одного збору редіректів клієнта (task-51 §3.6) — викликається

з `app/monitor/runner.py` після sitemap, у light І full прогонах (дешевше за
будь-яку сторінку: один запит у pull-режимі). Визначає джерело, тягне
знімок, діфає проти збереженого стану, персистить `MonitorRedirect`-рядки +
`MonitorResource(kind="redirects")`-знімок цілком, класифікує серйозність і
пише `MonitorChange`.

Єдине місце, де три чисті модулі (`wp_redirection`/`probe`/`detect`,
`diff`, `severity`) зустрічаються з БД. Решта пакета про сесію/репозиторій не
знає нічого (`app/monitor/redirects/__init__.py`).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.core.config import Settings
from app.db.models import Client, MonitorSettings
from app.db.repositories.monitor import MonitorRepository
from app.monitor import diff as diffmod
from app.monitor import severity as sev
from app.monitor import storage
from app.monitor.redirects import detect as detectmod
from app.monitor.redirects import probe as probemod
from app.monitor.redirects import wp_browser, wp_redirection
from app.monitor.redirects.contract import RedirectRule, RedirectSourceResult
from app.monitor.redirects.credentials import load_credentials, load_login_credentials
from app.monitor.redirects.diff import diff_redirect_rules
from app.monitor.redirects.fingerprint import rule_fingerprint
from app.monitor.redirects.severity import (
    classify_added_rule,
    is_bulk,
    is_offsite_target,
    severity_of_redirect_change,
)

RESOURCE_KIND = "redirects"
REDIRECTS_UNAVAILABLE_TRACKED = frozenset({"redirects_unavailable"})
# Не re-detect'имо джерело щогодини — анонімний запит, але зайвий на кожен
# light-прогін; раз на добу достатньо (плагін не зникає й не зʼявляється
# частіше).
DETECT_CACHE_SECONDS = 86_400


@dataclass(frozen=True)
class RedirectsOutcome:
    ok: bool
    added: int = 0
    changed: int = 0
    removed: int = 0
    unavailable_reason: str | None = None


def _apply_redirect_changes(
    repo: MonitorRepository,
    client_id: str,
    run_id: str,
    changes: list[diffmod.DetectedChange],
    *,
    now: datetime,
) -> None:
    """Той самий дедуп/severity-escalation алгоритм, що `runner._apply_changes`

    (task-35 §3.4) — не імпортується звідти напряму, щоб не заводити
    циклічний імпорт `runner.py` <-> `collect.py` заради приватної функції;
    обидві версії викликають ті самі публічні методи репозиторію.
    """
    for change in changes:
        existing = repo.open_change(client_id, change.change_type, change.subject)
        if existing is not None:
            if existing.severity == change.severity:
                continue
            repo.resolve_change(existing, now=now)
        repo.record_change(
            client_id,
            run_id=run_id,
            severity=change.severity,
            change_type=change.change_type,
            scope=change.scope,
            subject=change.subject,
            related_id=change.related_id,
            old_value=change.old_value,
            new_value=change.new_value,
            details=change.details,
            now=now,
        )


def _reconcile_redirect_changes(
    repo: MonitorRepository,
    client_id: str,
    run_id: str,
    subject: str,
    *,
    tracked_types: frozenset[str],
    detected: list[diffmod.DetectedChange],
    now: datetime,
) -> None:
    detected_types = {c.change_type for c in detected if c.subject == subject}
    for change_type in tracked_types - detected_types:
        open_change = repo.open_change(client_id, change_type, subject)
        if open_change is None:
            continue
        repo.resolve_change(open_change, now=now)
        repo.record_change(
            client_id,
            run_id=run_id,
            severity=sev.INFO,
            change_type=sev.resolved_change_type(change_type),
            scope=open_change.scope,
            subject=subject,
            now=now,
        )


def _normalize(url: str) -> str:
    return url.rstrip("/")


def _known_live_urls(repo: MonitorRepository, client_id: str) -> set[str]:
    """A URL we would call "live" for offsite/on-live-url classification

    (task-51 §3.4): currently in the sitemap, or its last check answered
    2xx — either way not `gone_since`.
    """
    live = set()
    for page in repo.all_pages(client_id):
        if page.gone_since is not None:
            continue
        if page.in_sitemap or (page.status is not None and 200 <= page.status < 300):
            live.add(_normalize(page.url))
    return live


def _absolute(source_url: str, base_url: str) -> str:
    if source_url.startswith(("http://", "https://")):
        return source_url
    return f"{base_url.rstrip('/')}/{source_url.lstrip('/')}"


def _rule_source_is_live(rule: RedirectRule, *, base_url: str, known_live: set[str]) -> bool:
    return _normalize(_absolute(rule.source_url, base_url)) in known_live


def _probe_candidates(
    repo: MonitorRepository, client_id: str, base_url: str, *, key_pages: set[str], max_urls: int
) -> list[str]:
    """Known URLs to trace (task-51 §3.2.3): existing probe rules (so a

    target-change or a rule disappearing is still caught) + key pages, then
    padded from the sitemap in a stable (sorted) order — the same set every
    run, not a shuffled sample that would make the change feed noisy for no
    reason.
    """
    existing = {
        _absolute(rule.source_url, base_url)
        for rule in repo.list_redirects(client_id, source=probemod.SOURCE, state="active")
    }
    candidates = existing | {_absolute(url, base_url) for url in key_pages}
    if len(candidates) < max_urls:
        sitemap_urls = {p.url for p in repo.all_pages(client_id) if p.in_sitemap}
        for url in sorted(sitemap_urls):
            if len(candidates) >= max_urls:
                break
            candidates.add(url)
    return sorted(candidates)[:max_urls]


def _resolve_source(
    repo: MonitorRepository,
    client: Client,
    base_url: str,
    monitor_settings: MonitorSettings,
    *,
    now: datetime,
) -> str:
    choice = monitor_settings.redirect_source or "auto"
    if choice != "auto":
        return choice
    cached = monitor_settings.redirect_source_json or {}
    cached_source = cached.get("source")
    cached_at_raw = cached.get("checked_at")
    if cached_source and cached_at_raw:
        try:
            cached_at = datetime.fromisoformat(cached_at_raw)
        except ValueError:
            cached_at = None
        if cached_at is not None and (now - cached_at).total_seconds() < DETECT_CACHE_SECONDS:
            return str(cached_source)
    detected = detectmod.detect_source(base_url)
    monitor_settings.redirect_source_json = {
        "source": detected.value,
        "checked_at": now.isoformat(),
    }
    repo.commit()
    return detected.value


def _fetch_snapshot(
    repo: MonitorRepository,
    client: Client,
    base_url: str,
    effective_source: str,
    *,
    settings: Settings,
    key_pages: set[str],
) -> RedirectSourceResult:
    if effective_source == wp_redirection.SOURCE:
        credentials = load_credentials(settings.wp_credentials_path, client.id)
        if credentials is not None:
            result = wp_redirection.fetch_redirects(
                base_url, credentials, max_rules=settings.monitor_redirects_max_rules
            )
            if result.ok:
                return result
        else:
            result = None
        # Browser-login fallback (task-51 follow-up, live finding
        # 2026-08-19): no client in the portfolio had an Application
        # Password ever issued, so this is the path that actually works
        # today — tried whenever it's configured, whether or not an
        # Application Password is ALSO configured but failing.
        login_credentials = load_login_credentials(settings.wp_login_credentials_path, client.id)
        if login_credentials is not None:
            return wp_browser.login_and_fetch_redirects(
                base_url, login_credentials, max_rules=settings.monitor_redirects_max_rules
            )
        if result is not None:
            return result
        return RedirectSourceResult(
            ok=False,
            source=wp_redirection.SOURCE,
            unavailable_reason="auth_failed",
            detail="No Application Password or WordPress login configured for this client.",
        )
    candidates = _probe_candidates(
        repo,
        client.id,
        base_url,
        key_pages=key_pages,
        max_urls=settings.monitor_redirect_probe_max_urls,
    )
    return probemod.probe_redirects(candidates)


def _resource_summary(rules: tuple[RedirectRule, ...]) -> dict[str, Any]:
    by_code: dict[str, int] = {}
    by_group: dict[str, int] = {}
    for rule in rules:
        code_key = str(rule.http_code) if rule.http_code is not None else "unknown"
        by_code[code_key] = by_code.get(code_key, 0) + 1
        group_key = rule.group_name or "(none)"
        by_group[group_key] = by_group.get(group_key, 0) + 1
    return {
        "total": len(rules),
        "enabled": sum(1 for r in rules if r.status == "enabled"),
        "disabled": sum(1 for r in rules if r.status != "enabled"),
        "by_code": by_code,
        "by_group": by_group,
    }


def _persist_snapshot_resource(
    repo: MonitorRepository,
    client: Client,
    base_url: str,
    result: RedirectSourceResult,
    *,
    artifacts_dir: Any,
    now: datetime,
) -> None:
    """Whole-list snapshot as a `MonitorResource` (task-51 §3.1) — free reuse

    of the exact mechanism robots.txt/sitemaps already use: "View file" and
    the task-36 diff viewer work against it with zero new frontend code.
    """
    resource_url = f"{base_url}/__monitor_redirects__"
    raw = result.raw_snapshot
    if raw is None:
        # probe mode has no single canonical response body — synthesize one
        # so "View file"/history stays available for every source alike.
        raw = json.dumps(
            [
                {
                    "rule_key": r.rule_key,
                    "source_url": r.source_url,
                    "target_url": r.target_url,
                    "http_code": r.http_code,
                    "status": r.status,
                    "title": r.title,
                }
                for r in result.rules
            ],
            indent=2,
        ).encode("utf-8")

    resource = repo.get_resource(client.id, resource_url)
    old_content_sha256 = resource.content_sha256 if resource else None

    blob = storage.store_blob(artifacts_dir, raw)
    repo.record_blob(
        blob.sha256,
        size_bytes=blob.size_bytes,
        gzip_size_bytes=blob.gzip_size_bytes,
        mime="application/json",
        path=blob.path,
    )
    structure = _resource_summary(result.rules)
    resource = repo.upsert_resource_state(
        client.id,
        resource_url,
        kind=RESOURCE_KIND,
        status=200,
        content_sha256=blob.sha256,
        structure=structure,
        missing=False,
        now=now,
    )
    if blob.sha256 != old_content_sha256:
        prev = repo.latest_resource_version(resource.id)
        repo.create_resource_version(
            resource,
            http_status=200,
            content_sha256=blob.sha256,
            structure=structure,
            changed_fields=[],
            prev_version_id=prev.id if prev else None,
            observed_at=now,
        )


def process_redirects(
    repo: MonitorRepository,
    client: Client,
    base_url: str,
    *,
    run_id: str,
    monitor_settings: MonitorSettings,
    settings: Settings,
    artifacts_dir: Any,
    key_pages: set[str],
    now: datetime,
) -> RedirectsOutcome:
    if not settings.monitor_redirects_enabled or not monitor_settings.redirects_enabled:
        return RedirectsOutcome(ok=True)
    effective_source = _resolve_source(repo, client, base_url, monitor_settings, now=now)
    if effective_source == "off":
        return RedirectsOutcome(ok=True)

    subject = f"{base_url}/"
    previous_check_at = monitor_settings.redirects_last_checked_at
    is_baseline = previous_check_at is None

    result = _fetch_snapshot(
        repo, client, base_url, effective_source, settings=settings, key_pages=key_pages
    )

    if not result.ok:
        # `redirects_unavailable` fires even on the very first check — an
        # Application Password that never worked is exactly the fact an
        # operator needs to see immediately (same reasoning as `monitor_blocked`
        # in `runner.py`: no baseline gate for "we could not check at all").
        unavailable_changes = [
            diffmod.DetectedChange(
                "redirects_unavailable",
                severity_of_redirect_change("redirects_unavailable"),
                "site",
                subject,
                new_value=result.unavailable_reason,
                details={"detail": result.detail} if result.detail else {},
            )
        ]
        _apply_redirect_changes(repo, client.id, run_id, unavailable_changes, now=now)
        _reconcile_redirect_changes(
            repo,
            client.id,
            run_id,
            subject,
            tracked_types=REDIRECTS_UNAVAILABLE_TRACKED,
            detected=unavailable_changes,
            now=now,
        )
        repo.commit()
        return RedirectsOutcome(ok=False, unavailable_reason=result.unavailable_reason)

    existing_rows = {
        r.rule_key: r for r in repo.list_redirects(client.id, source=effective_source, state="all")
    }
    old_rules = {
        key: RedirectRule(
            rule_key=row.rule_key,
            source_url=row.source_url,
            target_url=row.target_url,
            http_code=row.http_code,
            match_type=row.match_type,
            regex=row.regex,
            status=row.status,
            group_name=row.group_name,
            title=row.title,
            external_id=row.external_id,
        )
        for key, row in existing_rows.items()
        if row.removed_at is None
    }
    new_rules = {rule.rule_key: rule for rule in result.rules}
    rule_diff = diff_redirect_rules(old_rules, new_rules)
    known_live = _known_live_urls(repo, client.id)

    changes: list[diffmod.DetectedChange] = []
    if not is_baseline:
        added_types = [
            classify_added_rule(
                offsite=is_offsite_target(entry.rule.target_url, client_domain=client.domain),
                on_live_url=_rule_source_is_live(
                    entry.rule, base_url=base_url, known_live=known_live
                ),
            )
            for entry in rule_diff.added
        ]
        if is_bulk(len(rule_diff.added), threshold=settings.monitor_redirects_bulk_threshold):
            by_type: dict[str, int] = {}
            for change_type in added_types:
                by_type[change_type] = by_type.get(change_type, 0) + 1
            changes.append(
                diffmod.DetectedChange(
                    "redirects_bulk_added",
                    severity_of_redirect_change("redirects_bulk_added"),
                    "site",
                    subject,
                    new_value=f"{len(rule_diff.added)} new rules",
                    details={
                        "count": len(rule_diff.added),
                        "by_type": by_type,
                        "examples": [e.rule.source_url for e in rule_diff.added[:10]],
                    },
                )
            )
        else:
            for entry, change_type in zip(rule_diff.added, added_types, strict=True):
                rule = entry.rule
                changes.append(
                    diffmod.DetectedChange(
                        change_type,
                        severity_of_redirect_change(change_type),
                        "site",
                        subject,
                        new_value=f"{rule.source_url} -> {rule.target_url or '(no target)'}",
                        details={
                            "source_url": rule.source_url,
                            "target_url": rule.target_url,
                            "http_code": rule.http_code,
                            "group": rule.group_name,
                            "window_start": previous_check_at.isoformat()
                            if previous_check_at
                            else None,
                            "window_end": now.isoformat(),
                        },
                    )
                )

        for entry in rule_diff.changed:
            rule, previous = entry.rule, entry.previous
            changes.append(
                diffmod.DetectedChange(
                    "redirect_target_changed",
                    severity_of_redirect_change("redirect_target_changed"),
                    "site",
                    subject,
                    old_value=(
                        f"{previous.target_url or '(no target)'} ({previous.http_code})"
                        if previous
                        else None
                    ),
                    new_value=f"{rule.target_url or '(no target)'} ({rule.http_code})",
                    details={"source_url": rule.source_url},
                )
            )

        for entry in rule_diff.removed:
            rule = entry.rule
            changes.append(
                diffmod.DetectedChange(
                    "redirect_removed",
                    severity_of_redirect_change("redirect_removed"),
                    "site",
                    subject,
                    old_value=f"{rule.source_url} -> {rule.target_url or '(no target)'}",
                    details={"source_url": rule.source_url},
                )
            )

    _apply_redirect_changes(repo, client.id, run_id, changes, now=now)
    # A successful check always reconciles `redirects_unavailable` — an
    # empty `changes` (or one with no such entry) closes out any previously
    # open one, the same self-healing every other tracked type gets
    # (task-35 §3.4). The failure branch above never reaches this line.
    _reconcile_redirect_changes(
        repo,
        client.id,
        run_id,
        subject,
        tracked_types=REDIRECTS_UNAVAILABLE_TRACKED,
        detected=changes,
        now=now,
    )

    for key, rule in new_rules.items():
        # A rule absent from `old_rules` is "new" for diff purposes whether
        # it never existed before OR it existed but was `removed_at`-marked
        # (`existing_rows` includes removed rows; `old_rules` deliberately
        # does not — see above). Either way this appearance gets its own
        # fresh window, not the row's ancient original `first_seen_at`.
        window_start = None if key in old_rules else previous_check_at
        repo.upsert_redirect_state(
            client.id,
            effective_source,
            key,
            source_url=rule.source_url,
            target_url=rule.target_url,
            http_code=rule.http_code,
            match_type=rule.match_type,
            regex=rule.regex,
            status=rule.status,
            group_name=rule.group_name,
            title=rule.title,
            external_id=rule.external_id,
            rule_sha256=rule_fingerprint(rule),
            now=now,
            window_start=window_start,
        )
    repo.mark_redirects_removed(client.id, effective_source, set(new_rules.keys()), now=now)
    _persist_snapshot_resource(repo, client, base_url, result, artifacts_dir=artifacts_dir, now=now)

    monitor_settings.redirects_last_checked_at = now
    repo.commit()

    return RedirectsOutcome(
        ok=True,
        added=len(rule_diff.added),
        changed=len(rule_diff.changed),
        removed=len(rule_diff.removed),
    )
