"""Thin wrapper over `b2sdk.v2` — the ONLY module in this codebase that
imports it (task-52 §3.2). Everything else in `app/cloud/**` talks to
`CloudStore`, never to `b2sdk` directly.

Every operation live-tested against the project's real B2 account
(2026-08-19, `theyovth-vault`, an application key restricted to that one
bucket): `put_bytes`/`put_file` → `exists` → `copy` (server-side, used for
the weekly/monthly backup promotion, task-52 §3.5) → `get_bytes`/
`get_to_file` → `delete`. `bucket.get_bucket_by_name(...)` is used to
resolve the bucket, never `api.list_buckets()` — on a bucket-restricted key
that call raises `RestrictedBucket` (confirmed live), so anything that
enumerates the account instead of asking for one named bucket would break
the moment this ships with a real restricted key.
"""

from __future__ import annotations

import io
import logging
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, TypeVar

from app.core.config import Settings
from app.core.logging import add_redact_keys

if TYPE_CHECKING:
    from b2sdk.v2 import B2Api, Bucket

_T = TypeVar("_T")

logger = logging.getLogger(__name__)

# Field/env names that could carry a B2 secret through a log call somewhere
# up the stack (PLAN §13/§14: secrets never reach logs/DB/UI). Registered at
# import time, same pattern as `app/monitor/redirects/credentials.py`.
add_redact_keys(
    {
        "b2_application_key",
        "b2_vault_application_key",
        "b2_master_application_key",
        "application_key",
    }
)

_MAX_ATTEMPTS = 3
_RETRY_DELAYS_SECONDS = (0.0, 1.0, 3.0)


class CloudDisabledError(RuntimeError):
    """Raised when a `CloudStore` operation runs while `cloud_enabled=False`."""


class CloudMisconfiguredError(RuntimeError):
    """`cloud_enabled=True` but the B2_VAULT_* credentials are incomplete.

    In practice `Settings.validate_cloud_credentials` already refuses to
    start in this state — this exists as a second, narrower guard for
    anything that builds a `CloudStore` directly against a hand-built
    `Settings` (tests, one-off scripts) rather than `get_settings()`.
    """


@dataclass(frozen=True)
class CloudObjectRef:
    key: str
    size_bytes: int
    sha1: str | None = None


class CloudStore:
    """Created once per process from `Settings`; authorizes lazily on first
    use so `cloud_enabled=False` never touches the network at all."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._api: B2Api | None = None
        self._bucket: Bucket | None = None

    @property
    def enabled(self) -> bool:
        return self._settings.cloud_enabled

    def _require_enabled(self) -> None:
        if not self.enabled:
            raise CloudDisabledError("cloud_enabled=False")

    def _bucket_handle(self) -> Bucket:
        if self._bucket is not None:
            return self._bucket
        settings = self._settings
        if not (
            settings.b2_vault_bucket_name
            and settings.b2_vault_key_id
            and settings.b2_vault_application_key
        ):
            raise CloudMisconfiguredError(
                "cloud_enabled=True, але B2_VAULT_BUCKET_NAME/B2_VAULT_KEY_ID/"
                "B2_VAULT_APPLICATION_KEY не задані повністю"
            )
        from b2sdk.v2 import B2Api, InMemoryAccountInfo

        api = B2Api(InMemoryAccountInfo())
        api.authorize_account(
            "production", settings.b2_vault_key_id, settings.b2_vault_application_key
        )
        bucket = api.get_bucket_by_name(settings.b2_vault_bucket_name)
        self._api = api
        self._bucket = bucket
        logger.info("cloud_store_authorized bucket=%s", settings.b2_vault_bucket_name)
        return bucket

    def _retrying(self, op_name: str, fn: Callable[[], _T]) -> _T:
        from b2sdk.v2.exception import BadRequest, Unauthorized

        last_exc: Exception | None = None
        for attempt, delay in enumerate(_RETRY_DELAYS_SECONDS[:_MAX_ATTEMPTS], start=1):
            if delay:
                time.sleep(delay)
            try:
                return fn()
            except (Unauthorized, BadRequest):
                # Configuration problems, not transient network faults —
                # retrying a bad key or a malformed request never helps.
                raise
            except Exception as exc:  # noqa: BLE001 - transport/service errors retried uniformly
                last_exc = exc
                logger.warning(
                    "cloud_%s_retry attempt=%d/%d error=%s",
                    op_name,
                    attempt,
                    _MAX_ATTEMPTS,
                    type(exc).__name__,
                )
        assert last_exc is not None  # noqa: S101 - loop above always runs >=1 iteration
        raise last_exc

    def put_file(self, path: Path, key: str, *, content_type: str | None = None) -> CloudObjectRef:
        self._require_enabled()
        bucket = self._bucket_handle()
        file_version = self._retrying(
            "put_file", lambda: bucket.upload_local_file(str(path), key, content_type=content_type)
        )
        return CloudObjectRef(
            key=key, size_bytes=file_version.size, sha1=getattr(file_version, "content_sha1", None)
        )

    def put_bytes(
        self, data: bytes, key: str, *, content_type: str | None = None
    ) -> CloudObjectRef:
        self._require_enabled()
        bucket = self._bucket_handle()
        file_version = self._retrying(
            "put_bytes", lambda: bucket.upload_bytes(data, key, content_type=content_type)
        )
        return CloudObjectRef(
            key=key, size_bytes=file_version.size, sha1=getattr(file_version, "content_sha1", None)
        )

    def get_bytes(self, key: str) -> bytes:
        self._require_enabled()
        bucket = self._bucket_handle()

        def _do() -> bytes:
            buf = io.BytesIO()
            bucket.download_file_by_name(key).save(buf)
            return buf.getvalue()

        return self._retrying("get_bytes", _do)

    def get_to_file(self, key: str, dest: Path) -> None:
        self._require_enabled()
        bucket = self._bucket_handle()

        def _do() -> None:
            dest.parent.mkdir(parents=True, exist_ok=True)
            bucket.download_file_by_name(key).save_to(str(dest))

        self._retrying("get_to_file", _do)

    def exists(self, key: str) -> CloudObjectRef | None:
        self._require_enabled()
        bucket = self._bucket_handle()
        from b2sdk.v2.exception import FileNotPresent

        try:
            info = self._retrying("exists", lambda: bucket.get_file_info_by_name(key))
        except FileNotPresent:
            return None
        return CloudObjectRef(key=key, size_bytes=info.size, sha1=info.content_sha1)

    def copy(self, src_key: str, dest_key: str) -> CloudObjectRef:
        """Server-side copy — no bytes travel through this process. Used to
        promote a daily backup into the weekly/monthly tier (task-52 §3.5)
        without re-uploading 100+ MB that's already in the bucket.
        """
        self._require_enabled()
        bucket = self._bucket_handle()
        from b2sdk.v2.exception import FileNotPresent

        src_info = bucket.get_file_info_by_name(src_key)
        if src_info is None:
            raise FileNotPresent(src_key)
        file_version = self._retrying("copy", lambda: bucket.copy(src_info.id_, dest_key))
        return CloudObjectRef(
            key=dest_key,
            size_bytes=file_version.size,
            sha1=getattr(file_version, "content_sha1", None),
        )

    def delete(self, key: str) -> None:
        self._require_enabled()
        bucket = self._bucket_handle()
        from b2sdk.v2.exception import FileNotPresent

        try:
            info = bucket.get_file_info_by_name(key)
        except FileNotPresent:
            return
        self._retrying("delete", info.delete)

    def list_prefix(self, prefix: str) -> Iterator[CloudObjectRef]:
        self._require_enabled()
        bucket = self._bucket_handle()
        for file_version, _folder_name in bucket.ls(prefix, recursive=True):
            yield CloudObjectRef(
                key=file_version.file_name,
                size_bytes=file_version.size,
                sha1=getattr(file_version, "content_sha1", None),
            )
