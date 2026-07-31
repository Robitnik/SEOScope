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


@lru_cache
def get_settings() -> Settings:
    return Settings()
