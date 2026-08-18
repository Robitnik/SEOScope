"""Reporting Ninja REST API client (task-42 §1.7/§2.1).

Ported from `AISeoTools/.../clientreport/api_client.py`, trimmed to what
`api_source.py` actually needs (no cursor pagination — GA4/GSC responses
here are a handful of channel rows or one aggregate row, never near the
10k-row page size) and with two additive fixes from live measurement:

- `INTERNAL` is retried like a transient 502/503/504 (§1.7: "`/query` без
  розмірності інколи віддає `INTERNAL: There was an internal error` — і той
  самий запит через секунду проходить").
- `request_count` — a plain counter the caller can read after a run, so a
  run's events can report "N Reporting Ninja requests" against the
  documented 300/min, 5000/day limits (§1.7) without wiring a second
  tracking mechanism.
"""

from __future__ import annotations

import threading
import time
from typing import Any

import httpx

from app.core.config import get_settings

BASE_URL = "https://api.reportingninja.com/v1"

_RETRYABLE_HTTP_STATUS = (502, 503, 504)
_RETRYABLE_ERROR_CODES = {"INTERNAL"}


class _TokenBucket:
    """Plain thread-safe token bucket — `reports_api_max_rpm` (task-47
    §1.6/§2.3). The account's own ceiling is 300 req/min and a single 14-
    client batch alone issues ~280 of them (task-47 §1.6, measured live), so
    the limiter has to be shared across every concurrent
    `ReportingNinjaClient` instance in the process, not per-instance — the
    limit is on the ACCOUNT, not on one run.
    """

    def __init__(self, rate_per_minute: float) -> None:
        self._capacity = max(1.0, rate_per_minute)
        self._tokens = self._capacity
        self._rate_per_second = self._capacity / 60.0
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                elapsed = now - self._last
                self._last = now
                self._tokens = min(self._capacity, self._tokens + elapsed * self._rate_per_second)
                if self._tokens >= 1:
                    self._tokens -= 1
                    return
                wait = (1 - self._tokens) / self._rate_per_second
            time.sleep(wait)


_bucket_lock = threading.Lock()
_bucket: _TokenBucket | None = None


def _shared_rate_limiter() -> _TokenBucket:
    global _bucket
    with _bucket_lock:
        if _bucket is None:
            _bucket = _TokenBucket(get_settings().reports_api_max_rpm)
        return _bucket


def reset_rate_limiter_for_tests(rate_per_minute: float | None = None) -> None:
    """Test-only escape hatch — the bucket is a module-level singleton so a
    test that wants a tight, deterministic limit (or a fresh full bucket)
    cannot just construct a new client.
    """
    global _bucket
    with _bucket_lock:
        _bucket = None if rate_per_minute is None else _TokenBucket(rate_per_minute)


class RateLimitError(Exception):
    def __init__(self, retry_after: float) -> None:
        self.retry_after = retry_after
        super().__init__(f"Rate limited — retry after {retry_after:.0f}s")


class APIError(Exception):
    def __init__(self, error_code: str, message: str) -> None:
        self.error_code = error_code
        super().__init__(f"{error_code}: {message}")


class ReportingNinjaClient:
    def __init__(self, api_key: str, *, timeout_seconds: float = 30.0) -> None:
        if not api_key:
            raise ValueError("Reporting Ninja API key is required")
        self.request_count = 0
        self._http = httpx.Client(
            base_url=BASE_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            timeout=timeout_seconds,
        )

    def _request(self, path: str, body: dict[str, Any], *, retries: int = 3) -> dict[str, Any]:
        """POST to `path`, retrying on 429 (`Retry-After`), transient upstream
        5xx, and the `INTERNAL` application-level error — bounded exponential
        backoff. 400/401/403 and any other `error_code` are NOT retried: a
        bad request or an auth failure will not fix itself on a retry.
        """
        last_internal_error: APIError | None = None
        for attempt in range(retries):
            _shared_rate_limiter().acquire()
            self.request_count += 1
            response = self._http.post(path, json=body)

            if response.status_code == 429:
                retry_after = float(response.headers.get("Retry-After", 60))
                if attempt < retries - 1:
                    time.sleep(retry_after)
                    continue
                raise RateLimitError(retry_after)

            if response.status_code in _RETRYABLE_HTTP_STATUS and attempt < retries - 1:
                time.sleep(2**attempt * 3)
                continue

            response.raise_for_status()
            data: dict[str, Any] = response.json()

            if data.get("status") == "error":
                error_code = data.get("error_code", "UNKNOWN")
                if error_code in _RETRYABLE_ERROR_CODES and attempt < retries - 1:
                    last_internal_error = APIError(error_code, data.get("message", str(data)))
                    time.sleep(2**attempt * 2)
                    continue
                raise APIError(error_code, data.get("message", str(data)))

            payload: dict[str, Any] = data.get("data", {})
            return payload

        if last_internal_error is not None:
            raise last_internal_error
        raise RuntimeError(f"Failed after {retries} attempts: {path}")

    def list_fields(self, integration_id: str) -> list[dict[str, Any]]:
        data = self._request("/fields", {"integration_id": integration_id})
        fields: list[dict[str, Any]] = data.get("fields", [])
        return fields

    def query(
        self,
        integration_id: str,
        connection_key: str,
        account_id: str,
        fields: list[str],
        date_start: str,
        date_end: str,
        *,
        limit: int = 100,
        settings: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        body: dict[str, Any] = {
            "integration_id": integration_id,
            "connection_key": connection_key,
            "account_id": account_id,
            "fields": fields,
            "date_range": {"preset": "custom", "start": date_start, "end": date_end},
            "limit": limit,
        }
        if settings:
            body["settings"] = settings
        data = self._request("/query", body)
        rows: list[dict[str, Any]] = data.get("rows", [])
        return rows

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> ReportingNinjaClient:
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()
