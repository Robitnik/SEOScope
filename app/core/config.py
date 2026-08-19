"""Application settings (pydantic-settings), loaded from environment / .env."""

from __future__ import annotations

from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Self

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="",
        extra="ignore",
    )

    app_name: str = "The Yovth Clients Report"
    environment: str = "development"
    log_level: str = "INFO"

    database_url: str = "sqlite+aiosqlite:///./app_data/app.db"

    artifacts_dir: Path = Path("./app_data/artifacts")
    browser_profiles_dir: Path = Path("./app_data/browser_profiles")
    # Read-only checkout mounted by compose. Provider code, clients.json and
    # historical artifacts remain owned by AISeoTools.
    aiseotools_source_path: Path = Path("/seo/AISeoTools")

    frontend_dir: Path = Path("./frontend")

    # Browser session bridge (task-20, PLAN §8.3/§13/§17). The host name the container uses to
    # reach a warm Chrome's CDP port. Compose runs api/worker with `network_mode: host` (Chrome's
    # CDP socket is loopback-only — see compose.yaml), so this is 127.0.0.1 by default, not a
    # docker-bridge hostname. Never a host filesystem path (PLAN §14). This setting only documents
    # the value for the API layer; the actual liveness switch is the SEOTOOLS_REMOTE_CDP_HOST env
    # var compose sets for AISeoTools directly (zero AISeoTools import here).
    chrome_cdp_host: str = "127.0.0.1"
    browser_login_timeout_seconds: float = 120.0

    event_retention_days: int = 30
    job_lease_seconds: int = 300

    # How many cells of ONE provider may collect at the same time (task-31).
    # For ahrefs/semrush this is literally how many windows of the warm host
    # Chrome are driven in parallel on the single logged-in profile.
    #
    # Capped at 4 on purpose: the ceiling is not CPU but the provider — more
    # concurrent sessions from one account look increasingly like automation,
    # and metered units burn N times faster in wall-clock time. `tech_audit`
    # and `blogs` stay at 1 because each already parallelises its own crawl.
    browser_parallel_slots: int = 3
    max_browser_parallel_slots: int = 4
    # Per-provider overrides, set from MEASUREMENT rather than taste
    # (LIVE 2026-07-31, 3 clients each, warm host Chrome, one profile):
    #
    #   ahrefs @ 3 lanes -> KPIs fine, but ALL THREE detail exports timed out
    #                       (`Ahrefs detail export failed: TimeoutError`), so
    #                       every snapshot came back `partial`.
    #   ahrefs @ 2 lanes -> both cells `succeeded`, quality `complete`, no
    #                       warnings, ~100s wall clock for two clients.
    #
    # The cause is structural, not tuning: the bulk export goes through the
    # store-wide `download_lock` (CDP `Browser.setDownloadBehavior` is
    # browser-global — see `seo_audit/browser/profile.py`), so a third lane adds
    # queueing to a phase that cannot run in parallel and pushes the waiting
    # lanes past their Playwright timeouts. Semrush collects almost entirely by
    # export and shares that same lock, so it gets the same ceiling.
    provider_parallel_slots: dict[str, int] = {"ahrefs": 2, "semrush": 2}
    # Seconds between the starts of a provider's lanes. LIVE 2026-07-31: with
    # all three starting in the same millisecond, two of three Ahrefs cells died
    # on a 60s `Page.goto` timeout — three identical heavy SPA loads through one
    # Chrome process — while the third finished normally. Spacing the starts
    # removes that thundering herd; after the first cell the lanes drift apart on
    # their own. Purely a smoothing knob: concurrency itself is bounded by the
    # capacity semaphore, not by this.
    lane_start_stagger_seconds: float = 8.0
    single_lane_providers: list[str] = ["tech_audit", "blogs"]
    http_request_timeout_seconds: float = 30.0

    cors_allow_origins: list[str] = []

    # Alerts engine thresholds (task-17, PLAN §21.5) — noise-control knobs,
    # deliberately configurable rather than hardcoded in app.metrics.alerts.
    alert_min_relative_change_pct: Decimal = Decimal("20")
    alert_min_base_impressions: int = 50
    alert_min_base_clicks: int = 10
    alert_top_n_per_group: int = 20

    # Site Monitor (task-35, PLAN §16 "Етап 8"). Values are measurements, not
    # taste — see task-35 §1.4/§3.3/§3.7 for the live portfolio numbers behind
    # each default.
    monitor_enabled: bool = True
    monitor_light_interval_hours: float = 1.0
    monitor_full_interval_hours: float = 3.0
    monitor_client_workers: int = 4
    # Safety valve, NOT a product limit (§3.3): the largest client today is 608
    # URLs. This exists only so a runaway/looping sitemap cannot hang the
    # engine forever.
    monitor_max_urls_per_client: int = 25_000
    monitor_run_max_minutes: float = 90.0
    monitor_max_body_bytes: int = 3 * 1024 * 1024
    monitor_request_timeout_seconds: float = 20.0
    monitor_host_min_interval_seconds: float = 0.25
    monitor_key_pages_limit: int = 10
    # Confirmed with the user 2026-08-11: a URL gone from the sitemap stays
    # watched for 30 days before it drops off entirely, so a silent removal
    # is still caught as `page_gone` instead of vanishing unnoticed.
    monitor_gone_retention_days: int = 30
    # Confirmed with the user 2026-08-11: full page bodies are kept forever by
    # default (§3.2.1) — retention is an explicit opt-in via `monitor-prune
    # --bodies-older-than`, never a silent default.
    monitor_store_page_bodies: bool = True
    monitor_body_retention_days: int = 0
    # We deliberately do NOT honor the client's own robots.txt (§3.3): the
    # moment robots.txt closes the site is exactly the incident we exist to
    # catch, and obeying the ban we are supposed to be watching for is a
    # logical trap. `Crawl-delay` is still respected.
    monitor_respect_robots: bool = False
    monitor_run_stale_minutes: float = 60.0
    monitor_doh_resolvers: list[str] = ["dns.google", "cloudflare-dns.com"]
    monitor_sitemap_drop_alert_pct: Decimal = Decimal("10")
    monitor_sitemap_broken_warn_pct: Decimal = Decimal("2")
    monitor_sitemap_broken_warn_count: int = 10
    monitor_sitemap_broken_alert_pct: Decimal = Decimal("10")
    monitor_sitemap_broken_alert_count: int = 50

    # Antibot lane (task-50, PLAN §12 "чесні стани"). Live-measured on
    # alphastyle.store (task-50 §0): 30 requests at 0.25s pass clean, ~58
    # requests wall solid — the exact safe throughput was deliberately NOT
    # determined here (finding it means driving a live client's site into a
    # multi-minute penalty window on purpose); `monitor_antibot_min_interval_
    # seconds` starts conservative and gets measured against the real first
    # antibot-lane run instead.
    monitor_antibot_workers: int = 1
    monitor_antibot_min_interval_seconds: float = 2.0
    # How many consecutive blocked pages end a run early rather than grinding
    # through the rest of the target list into the same wall (§0.6: 304 of
    # 362 requests in the incident run were spent hammering a wall already up
    # since request ~58).
    monitor_block_streak_abort: int = 5
    # `fetch_mode="auto"` clients stay on the browser lane for this many days
    # after the last confirmed block, not just the run that found it — a site
    # that blocks on one full run blocks on the next one too, and bouncing
    # back to plain HTTP every few hours would alternate clean runs with
    # corrupted ones instead of settling (task-50 §B5).
    monitor_antibot_sticky_days: float = 14.0

    # Redirect monitoring (task-51). Live-measured on the portfolio
    # 2026-08-19: 10 of 14 clients expose `redirection/v1` in `/wp-json/`
    # (authenticated pull), the rest fall back to the blackbox probe.
    monitor_redirects_enabled: bool = True
    # Above this many new rules in one run, collapse them into one
    # `redirects_bulk_added` aggregate instead of one row each (same pattern
    # as `monitor_sitemap_broken_warn_count`) — a bulk permalink change on an
    # active site is one human action, not N events.
    monitor_redirects_bulk_threshold: int = 25
    # Blackbox-probe fallback (task-51 §3.2.3): existing probe rules + key
    # pages, padded from the sitemap up to this many URLs — a request budget
    # roughly on par with the light run's own page checks.
    monitor_redirect_probe_max_urls: int = 50
    # Emergency valve on the pull adapter's pagination, not a product limit —
    # the largest portfolio client today is nowhere near this.
    monitor_redirects_max_rules: int = 20_000
    # Application Passwords, file-based and OUTSIDE the database (task-51
    # §3.3): the project has no encrypted-column mechanism, and
    # `ClientIntegration.config_json` is explicitly documented as
    # non-secret-only. `{"<client_id>": {"user": ..., "app_password": ...}}`,
    # `0600`, never written by this codebase's own migrations/seed data.
    wp_credentials_path: Path = Path("./app_data/secrets/wp_credentials.json")
    # Regular wp-admin login, browser-session fallback (task-51 follow-up,
    # live finding 2026-08-19): no client in the portfolio had an
    # Application Password ever issued. A REGULAR password is a bigger
    # secret than an app password (full account access, not one revocable
    # token) — kept in its OWN file so the two credential kinds are never
    # confused about which one a record holds.
    wp_login_credentials_path: Path = Path("./app_data/secrets/wp_login_credentials.json")

    # Reports engine (task-42, PLAN §16 "Хвиля N"). Defaults are measurements,
    # not taste — see task-42 §1.2/§1.5/§1.7/§3.6/§3.7 for the live numbers
    # behind each one.
    reports_enabled: bool = True
    reports_api_workers: int = 3
    # NOT to be raised: one login, one browser, one cabinet session per
    # process — a second concurrent PDF run would double-click the same UI.
    reports_pdf_workers: int = 1
    reports_max_months: int = 24
    reports_max_comparator_years: int = 4
    # Measured 41-67s per column headless; generous ceiling for a slow one.
    reports_pdf_period_timeout_seconds: float = 300.0
    reports_pdf_retries: int = 3
    # The API path had NO period-level retry at all before this (task-47
    # follow-up, live-found alongside the PDF path's own retry-classification
    # bug): one bad response failed the period outright. Mirrors
    # `reports_pdf_retries`'s shape/default.
    reports_api_period_retries: int = 3
    reports_cross_check_tolerance_pct: Decimal = Decimal("0.5")
    reports_pdf_cache_enabled: bool = True
    # `False` leaves a hook for local debugging under Xvfb.
    reports_headless: bool = True
    # How many COMPLETE months the "Historical Data" block covers (task-45
    # §2.4). 12 mirrors the hand-kept Google Sheets history it replaces; the
    # whole window costs two Reporting Ninja queries (~2s), so this is a
    # readability choice, not a cost one.
    reports_history_months: int = 12

    # Batch + client document/PDF (task-47). Defaults are measurements, not
    # taste — see task-47 §1.6/§2.3 for the live numbers behind each one.
    #
    # Separate from `reports_api_workers` on purpose (task-47 §2.3): without
    # its own pool, a 14-client batch would occupy every API-path slot and an
    # operator's unrelated single-client report would queue behind the whole
    # batch. `ReportScheduler` now runs standalone and batch-sourced API runs
    # in two independent pools of this size.
    reports_batch_client_workers: int = 3
    # Token-bucket ceiling on Reporting Ninja API calls, shared across the
    # whole process (task-47 §1.6/§2.3): the account's own limit is 300/min,
    # and a 14-client batch alone issues ~280 — without a limiter here, one
    # concurrent ad-hoc report on top of a running batch would exceed it.
    # 240 is 80% of the account ceiling, leaving headroom for exactly that.
    reports_api_max_rpm: float = 240.0
    # The API process's own base URL, reachable from the WORKER process too:
    # both run with `network_mode: host` (compose.yaml), so the worker can
    # render a client PDF by driving Chromium against the API's own
    # `/reports/runs/{id}/document` page (task-47 §2.1) exactly like a
    # browser would — one rendered markup, on screen and in the PDF alike.
    reports_internal_base_url: str = "http://127.0.0.1:6524"

    # Cloud storage (task-52, Backblaze B2): off-site DB backup, a mirror of
    # every client report file, and the Site Monitor web archive. Live-tested
    # 2026-08-19 against a PRIVATE bucket (`theyovth-vault`) with its own
    # restricted application key — see the task plan §1.2/§1.3 for why:
    # the account's original key was an unrestricted master key, and the
    # only OTHER bucket on this account (`TheYovth`) is `allPublic`, so
    # storing backups there would need an encryption key on our side whose
    # loss would make every backup unreadable forever (rejected by the user
    # 2026-08-19 as a worse risk than the private bucket).
    cloud_enabled: bool = False
    b2_vault_bucket_name: str = ""
    b2_vault_key_id: str = ""
    b2_vault_application_key: str = ""
    # UTC "HH:MM" — the one daily backup tick (`app/cloud/scheduler.py`).
    cloud_backup_at_utc: str = "03:30"
    cloud_backup_keep_daily: int = 30
    cloud_backup_keep_weekly: int = 12
    cloud_backup_keep_monthly: int = 12
    cloud_sync_reports: bool = True
    cloud_sync_monitor_blobs: bool = True
    # Per-tick sweep size (task-52 §3.6) — the portfolio's own measured
    # growth (300-800 new monitor blobs/day) is far below this even at the
    # scheduler's 60s tick, so this is a safety ceiling, not a throughput knob.
    cloud_sweep_batch_size: int = 200
    cloud_upload_workers: int = 4
    cloud_request_timeout_seconds: float = 60.0

    @model_validator(mode="after")
    def validate_cloud_credentials(self) -> Self:
        # `cloud_enabled=True` with missing credentials must fail loudly at
        # startup (task-52 §3.8) — a silent no-op would look identical to a
        # working off-site backup until the day it's actually needed.
        if self.cloud_enabled and not (
            self.b2_vault_bucket_name and self.b2_vault_key_id and self.b2_vault_application_key
        ):
            raise ValueError(
                "cloud_enabled=True вимагає всіх трьох B2_VAULT_BUCKET_NAME/"
                "B2_VAULT_KEY_ID/B2_VAULT_APPLICATION_KEY"
            )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
