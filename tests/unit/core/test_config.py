from pathlib import Path

from app.core.config import Settings


def test_settings_defaults_load_without_env(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    settings = Settings(_env_file=None)

    assert settings.app_name == "The Yovth Clients Report"
    assert settings.log_level == "INFO"
    assert settings.event_retention_days == 30
    assert settings.cors_allow_origins == []
    assert isinstance(settings.artifacts_dir, Path)


def test_settings_read_from_env(monkeypatch):
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///./custom.db")
    monkeypatch.setenv("EVENT_RETENTION_DAYS", "90")

    settings = Settings(_env_file=None)

    assert settings.log_level == "DEBUG"
    assert settings.database_url == "sqlite+aiosqlite:///./custom.db"
    assert settings.event_retention_days == 90
