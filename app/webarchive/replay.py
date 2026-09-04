"""Replay timestamp helpers (task-59 §1.5/§4).

Format mirrors Wayback's own convention exactly — 14-digit `YYYYMMDDHHMMSS`,
UTC — deliberately: it is a familiar, sortable, URL-safe format, and
`/archive/r/20260904193000/https://example.com/` reads like a real Wayback
URL to anyone who has ever used one.

Snapshot/asset-version RESOLUTION (given a url and a timestamp, which stored
version was live) needs a DB session and lives in
`app.db.repositories.web_archive.WebArchiveRepository` instead — this module
stays a pure, dependency-free helper so it is trivial to unit test.
"""

from __future__ import annotations

from datetime import UTC, datetime

TS_FORMAT = "%Y%m%d%H%M%S"
TS_LENGTH = 14


def format_ts(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime(TS_FORMAT)


def parse_ts(value: str) -> datetime | None:
    if len(value) != TS_LENGTH or not value.isdigit():
        return None
    try:
        return datetime.strptime(value, TS_FORMAT).replace(tzinfo=UTC)
    except ValueError:
        return None
