"""Application settings (pydantic-settings), loaded from environment / .env."""

from __future__ import annotations

from decimal import Decimal
from functools import lru_cache
from pathlib import Path

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
    reports_cross_check_tolerance_pct: Decimal = Decimal("0.5")
    reports_pdf_cache_enabled: bool = True
    # `False` leaves a hook for local debugging under Xvfb.
    reports_headless: bool = True
    reports_run_stale_minutes: float = 60.0
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


@lru_cache
def get_settings() -> Settings:
    return Settings()
