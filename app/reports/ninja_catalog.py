"""One cached snapshot of the Reporting Ninja account's report names.

The whole account is 32 reports for 16 clients and one clean cabinet pass
reads all of them in ~7 seconds (measured live 2026-09-07,
`list_report_names` → 32 names, `truncated=False`). That is the entire
input every per-client question needs:

- "which reports could belong to client X?" — the names starting with X;
- "does the account really have the name the operator picked?" — membership.

Before this module both questions launched their OWN Chromium, logged in
again and re-read the same cabinet: opening `Connect` on a row cost a
browser, and SAVING that row's answer cost another. Fifteen disconnected
clients therefore cost thirty logins, done one at a time because the
cabinet cannot be read concurrently at all (`ninja_browser.cabinet_lock`).
With one shared snapshot the first lookup pays ~7s and every later
lookup — including the save — is a dictionary read.

Deliberately file-backed, not an in-process dict: the api and worker are
separate processes, and an api restart must not make the operator wait for
a fresh cabinet pass again. The file holds nothing secret — report names
only, never credentials or the session cookie.

Staleness is honest rather than hidden. A snapshot older than the TTL is
refreshed on the next read; a name the caller cannot find in a snapshot is
never reported as absent until a REFRESHED snapshot also lacks it, because
"the operator created this report in Reporting Ninja two minutes ago" is
the ordinary case, not the exotic one.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from app.core.config import get_settings
from app.reports import ninja_browser

logger = logging.getLogger(__name__)

_DEFAULT_TTL_SECONDS = 6 * 60 * 60
_lock = threading.Lock()


def _ttl_seconds() -> float:
    raw = os.environ.get("REPORTING_NINJA_NAMES_TTL_SECONDS", "")
    try:
        return max(0.0, float(raw)) if raw else float(_DEFAULT_TTL_SECONDS)
    except ValueError:
        return float(_DEFAULT_TTL_SECONDS)


def _cache_path() -> Path:
    override = os.environ.get("REPORTING_NINJA_NAMES_CACHE_PATH", "")
    if override:
        return Path(override)
    return Path(
        os.environ.get(
            "SEOAUDIT_DASHBOARD_NINJA_SESSION_PATH", "/app/app_data/.ninja_session.json"
        )
    ).with_name(".ninja_report_names.json")


@dataclass(frozen=True)
class AccountReports:
    """`names` is every report name the cabinet showed on the pass at
    `fetched_at`; `truncated` marks a pass that ran out of scroll budget, and
    while it is set the absence of a name from `names` proves nothing
    (task-48 §1.7). `available=False` means the question could not be asked
    at all — no credentials configured — which is never evidence either.
    """

    names: list[str]
    fetched_at: float
    truncated: bool
    available: bool
    error: str | None = None

    @property
    def age_seconds(self) -> float:
        return max(0.0, time.time() - self.fetched_at) if self.fetched_at else 0.0

    @property
    def stale(self) -> bool:
        return not self.fetched_at or self.age_seconds > _ttl_seconds()

    def has(self, name: str) -> bool:
        return name.strip() in set(self.names)

    def proves_absent(self, name: str) -> bool:
        """Only a complete, successfully-read snapshot can prove a name is
        absent. Anything else — no credentials, a failed pass, a truncated
        scroll — is "could not verify", and a caller that rejects on it turns
        an unanswered question into a false `report_name_unknown`.
        """
        return self.available and self.error is None and not self.truncated and not self.has(name)


_UNAVAILABLE = AccountReports(names=[], fetched_at=0.0, truncated=True, available=False)


def _credentials() -> tuple[str, str] | None:
    email = os.environ.get("REPORTING_NINJA_EMAIL", "")
    password = os.environ.get("REPORTING_NINJA_PASSWORD", "")
    return (email, password) if email and password else None


def _read_cache() -> AccountReports | None:
    path = _cache_path()
    try:
        payload = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    names = payload.get("names")
    if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
        return None
    return AccountReports(
        names=names,
        fetched_at=float(payload.get("fetched_at") or 0.0),
        truncated=bool(payload.get("truncated", False)),
        available=True,
    )


def _write_cache(snapshot: AccountReports) -> None:
    path = _cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Write-then-rename: a reader in the other process never sees a
        # half-written list and mistakes it for a short account.
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(
                {
                    "names": snapshot.names,
                    "fetched_at": snapshot.fetched_at,
                    "truncated": snapshot.truncated,
                }
            )
        )
        tmp.replace(path)
    except OSError as exc:  # noqa: BLE001 - a cache that cannot be written is not a failure
        logger.warning("ninja_names_cache_write_failed error=%s", exc)


def _fetch() -> AccountReports:
    credentials = _credentials()
    if credentials is None:
        return _UNAVAILABLE
    email, password = credentials
    settings = get_settings()
    try:
        names, truncated = ninja_browser.list_report_names(
            email=email, password=password, headless=settings.reports_headless
        )
    except Exception as exc:  # noqa: BLE001 - classified by the caller, never a silent empty list
        logger.warning("ninja_names_fetch_failed error=%s", exc)
        return AccountReports(
            names=[], fetched_at=0.0, truncated=True, available=True, error=str(exc)
        )
    snapshot = AccountReports(
        names=sorted(set(names)), fetched_at=time.time(), truncated=truncated, available=True
    )
    _write_cache(snapshot)
    return snapshot


def get_account_reports(*, force_refresh: bool = False) -> AccountReports:
    """The account's report names, from cache unless it is stale or refused.

    Serialized on `_lock` so two simultaneous `Connect` clicks share ONE
    cabinet pass instead of queueing two behind `cabinet_lock` — the second
    click finds the snapshot the first one just wrote.
    """
    with _lock:
        # Credentials first, BEFORE the cache is even opened: without them
        # there is no cabinet to ask, and serving a cached account list while
        # reporting `available=False` would be answering a question the
        # process cannot actually answer.
        if _credentials() is None:
            return _UNAVAILABLE
        if not force_refresh:
            cached = _read_cache()
            if cached is not None and not cached.stale:
                return cached
        fresh = _fetch()
        if fresh.error is not None:
            # A failed refresh must not throw away a usable older answer: a
            # stale list of real names beats an empty one that reads as "the
            # account has no reports".
            cached = _read_cache()
            if cached is not None and cached.names:
                return AccountReports(
                    names=cached.names,
                    fetched_at=cached.fetched_at,
                    truncated=cached.truncated,
                    available=fresh.available,
                    error=fresh.error,
                )
        return fresh
