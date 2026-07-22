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
