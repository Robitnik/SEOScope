"""`CloudStore` (task-52 §3.2): the disabled/misconfigured guards and the
retry policy — never against real B2 here (see `scripts/live_smoke.py`'s
cloud section for the live-tested happy path, task-52 §5).
"""

from __future__ import annotations

import pytest

from app.cloud.client import CloudDisabledError, CloudMisconfiguredError, CloudStore
from app.core.config import Settings


def _enabled_settings() -> Settings:
    return Settings(
        _env_file=None,
        cloud_enabled=True,
        b2_vault_bucket_name="vault",
        b2_vault_key_id="key-id",
        b2_vault_application_key="secret",
    )


def test_disabled_store_raises_on_every_public_operation():
    store = CloudStore(Settings(_env_file=None, cloud_enabled=False))
    with pytest.raises(CloudDisabledError):
        store.put_bytes(b"x", "k")
    with pytest.raises(CloudDisabledError):
        store.get_bytes("k")
    with pytest.raises(CloudDisabledError):
        store.exists("k")
    with pytest.raises(CloudDisabledError):
        store.delete("k")
    with pytest.raises(CloudDisabledError):
        store.copy("a", "b")
    with pytest.raises(CloudDisabledError):
        list(store.list_prefix("p"))


def test_misconfigured_when_enabled_but_credentials_incomplete():
    # `Settings.validate_cloud_credentials` refuses this combination at
    # construction time — mutate AFTER construction (no `validate_assignment`
    # configured) to reach the narrower CloudStore-level guard this test
    # targets, which exists for anything that builds a `CloudStore` directly
    # against a hand-built `Settings` rather than `get_settings()`.
    settings = Settings(_env_file=None)
    settings.cloud_enabled = True
    store = CloudStore(settings)
    with pytest.raises(CloudMisconfiguredError):
        store.put_bytes(b"x", "k")


def test_retry_gives_up_after_max_attempts(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("app.cloud.client.time.sleep", lambda _seconds: None)
    store = CloudStore(_enabled_settings())
    calls = {"n": 0}

    def flaky() -> str:
        calls["n"] += 1
        raise ConnectionError("boom")

    with pytest.raises(ConnectionError):
        store._retrying("test_op", flaky)
    assert calls["n"] == 3


def test_retry_succeeds_after_transient_failures(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("app.cloud.client.time.sleep", lambda _seconds: None)
    store = CloudStore(_enabled_settings())
    calls = {"n": 0}

    def flaky() -> str:
        calls["n"] += 1
        if calls["n"] < 2:
            raise ConnectionError("boom")
        return "ok"

    assert store._retrying("test_op", flaky) == "ok"
    assert calls["n"] == 2


def test_retry_never_retries_unauthorized(monkeypatch: pytest.MonkeyPatch):
    from b2sdk.v2.exception import Unauthorized

    monkeypatch.setattr("app.cloud.client.time.sleep", lambda _seconds: None)
    store = CloudStore(_enabled_settings())
    calls = {"n": 0}

    def bad_auth() -> str:
        calls["n"] += 1
        raise Unauthorized("nope", "unauthorized")

    with pytest.raises(Unauthorized):
        store._retrying("test_op", bad_auth)
    assert calls["n"] == 1


def test_retry_never_retries_bad_request(monkeypatch: pytest.MonkeyPatch):
    from b2sdk.v2.exception import BadRequest

    monkeypatch.setattr("app.cloud.client.time.sleep", lambda _seconds: None)
    store = CloudStore(_enabled_settings())
    calls = {"n": 0}

    def malformed() -> str:
        calls["n"] += 1
        raise BadRequest("bad params", "bad_request")

    with pytest.raises(BadRequest):
        store._retrying("test_op", malformed)
    assert calls["n"] == 1


def test_secrets_never_appear_in_the_error_string(monkeypatch: pytest.MonkeyPatch):
    """A CloudMisconfiguredError/exception message is what an operator could
    see in a log line — it must describe the problem, never carry a key."""
    settings = Settings(_env_file=None)
    settings.cloud_enabled = True
    settings.b2_vault_application_key = "super-secret-value"
    settings.b2_vault_bucket_name = ""  # still incomplete -> misconfigured
    store = CloudStore(settings)
    with pytest.raises(CloudMisconfiguredError) as excinfo:
        store.put_bytes(b"x", "k")
    assert "super-secret-value" not in str(excinfo.value)
