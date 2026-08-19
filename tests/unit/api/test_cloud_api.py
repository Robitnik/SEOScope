"""`GET /api/v1/cloud/status` (task-52 §3.10) — read-only, no secrets ever
in the response, even when the request is what happened to trigger a
`Settings` reload that just picked up a real key from the environment.
"""

from __future__ import annotations

from app.core.config import get_settings
from app.db.repositories.cloud import CloudLedgerRepository


def test_status_disabled_by_default(client, monkeypatch):
    # Isolate from whatever B2_VAULT_* this MACHINE's real `.env` happens to
    # carry (task-52 configured real vault credentials there for live
    # testing) — this test asserts the OFF state specifically, and an OS env
    # var (unlike `monkeypatch.delenv`, which cannot touch the dotenv FILE
    # source) is what actually overrides a dotenv-sourced value.
    monkeypatch.setenv("CLOUD_ENABLED", "false")
    monkeypatch.setenv("B2_VAULT_BUCKET_NAME", "")
    monkeypatch.setenv("B2_VAULT_KEY_ID", "")
    monkeypatch.setenv("B2_VAULT_APPLICATION_KEY", "")
    get_settings.cache_clear()
    try:
        response = client.get("/api/v1/cloud/status")
    finally:
        get_settings.cache_clear()

    assert response.status_code == 200
    body = response.json()
    assert body["enabled"] is False
    assert body["bucket"] is None
    assert body["last_backup"] is None
    assert body["archive_since"] is None
    assert body["backups"] == {"daily": 0, "weekly": 0, "monthly": 0}


def test_status_reports_last_backup_and_tier_counts_when_enabled(client, db, monkeypatch):
    monkeypatch.setenv("CLOUD_ENABLED", "true")
    monkeypatch.setenv("B2_VAULT_BUCKET_NAME", "theyovth-vault")
    monkeypatch.setenv("B2_VAULT_KEY_ID", "key-id")
    monkeypatch.setenv("B2_VAULT_APPLICATION_KEY", "secret")
    get_settings.cache_clear()
    try:
        ledger = CloudLedgerRepository(db)
        ledger.record_backup(
            tier="daily",
            date_key="2026-08-19",
            key="db/daily/app-1.db.gz",
            sha256="x",
            size_bytes=100,
        )
        db.commit()

        response = client.get("/api/v1/cloud/status")
    finally:
        get_settings.cache_clear()

    assert response.status_code == 200
    body = response.json()
    assert body["enabled"] is True
    assert body["bucket"] == "theyovth-vault"
    assert body["last_backup"]["key"] == "db/daily/app-1.db.gz"
    assert body["backups"]["daily"] == 1
    assert body["queue"]["report_file"] == {"pending": 0, "failed": 0, "uploaded": 0}


def test_status_never_leaks_the_application_key(client, monkeypatch):
    monkeypatch.setenv("CLOUD_ENABLED", "true")
    monkeypatch.setenv("B2_VAULT_BUCKET_NAME", "theyovth-vault")
    monkeypatch.setenv("B2_VAULT_KEY_ID", "some-key-id")
    monkeypatch.setenv("B2_VAULT_APPLICATION_KEY", "top-secret-application-key-value")
    get_settings.cache_clear()
    try:
        response = client.get("/api/v1/cloud/status")
    finally:
        get_settings.cache_clear()

    assert "top-secret-application-key-value" not in response.text
    assert "some-key-id" not in response.text
