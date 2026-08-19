"""B2 object key layout (task-52 §3.1) — pure functions, no I/O.

```
db/daily/app-<timestamp>.db.gz            db/daily/app-<timestamp>.manifest.json
db/weekly/app-<timestamp>.db.gz           (same layout, promoted via CloudStore.copy)
db/monthly/app-<timestamp>.db.gz
reports/<client_slug>/<year>/<file_name>  (a -<sha256[:8]> suffix disambiguates a name collision)
archive/<sha256[:2]>/<sha256>.gz          (content-addressed, mirrors app.monitor.storage)
```
"""

from __future__ import annotations

BACKUP_TIERS = ("daily", "weekly", "monthly")


def db_backup_key(tier: str, timestamp: str) -> str:
    return f"db/{tier}/app-{timestamp}.db.gz"


def db_backup_manifest_key(tier: str, timestamp: str) -> str:
    return f"db/{tier}/app-{timestamp}.manifest.json"


def monitor_blob_key(sha256: str) -> str:
    """Mirrors `app.monitor.storage.blob_relative_path`'s local layout
    one-to-one, under `archive/` instead of `monitor/` — the sha256 IS the
    address, so a re-upload of the same bytes is inherently idempotent."""
    return f"archive/{sha256[:2]}/{sha256}.gz"


def report_object_key(
    client_slug: str, year: int, file_name: str, *, disambiguator: str | None = None
) -> str:
    """Human-readable key: an operator or the client themselves may one day
    browse this bucket directly. `disambiguator` (a short sha256 prefix)
    resolves a name collision without ever silently overwriting a different
    file that happens to share a name (task-52 §3.7)."""
    name = file_name
    if disambiguator:
        stem, dot, ext = file_name.rpartition(".")
        name = f"{stem}-{disambiguator}{dot}{ext}" if dot else f"{file_name}-{disambiguator}"
    return f"reports/{client_slug}/{year}/{name}"
