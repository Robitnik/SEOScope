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


# --- cloud storage (task-52 §3.8) ------------------------------------------


def test_cloud_disabled_by_default_needs_no_credentials():
    settings = Settings(_env_file=None)
    assert settings.cloud_enabled is False
    assert settings.b2_vault_bucket_name == ""


def test_cloud_enabled_with_full_credentials_is_valid():
    settings = Settings(
        _env_file=None,
        cloud_enabled=True,
        b2_vault_bucket_name="theyovth-vault",
        b2_vault_key_id="key-id",
        b2_vault_application_key="secret",
    )
    assert settings.cloud_enabled is True


def test_cloud_enabled_without_credentials_refuses_to_construct():
    import pytest

    with pytest.raises(ValueError, match="B2_VAULT"):
        Settings(_env_file=None, cloud_enabled=True)


def test_cloud_enabled_with_partial_credentials_refuses_to_construct():
    import pytest

    with pytest.raises(ValueError, match="B2_VAULT"):
        Settings(
            _env_file=None,
            cloud_enabled=True,
            b2_vault_bucket_name="theyovth-vault",
            # key_id/application_key deliberately missing
        )
