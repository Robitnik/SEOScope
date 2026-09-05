"""Content-addressed screenshot store for the URL Inspector (task-64 §2.3).

Its OWN root — `settings.artifacts_dir / "urlshots"` — deliberately NOT the
shared `monitor`/`archive` blob roots: `app.cloud.sweeper` prunes files under
those roots that carry no row in ITS OWN tables (`monitor_blobs`,
`archive_*`), and a screenshot written there without one of those rows is
exactly the "unreferenced blob" shape sweeper exists to delete. A separate
root makes that class of bug impossible by construction, at the cost of a
small resolver duplicated from `app.monitor.storage`/`app.webarchive.storage`
(same content-address + root-escape-guard shape, no gzip — JPEG is already
compressed).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

_ROOT_DIRNAME = "urlshots"


@dataclass(frozen=True)
class StoredShot:
    sha256: str
    size_bytes: int
    path: str  # relative to the artifacts root


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def shot_relative_path(sha256: str) -> str:
    return f"{_ROOT_DIRNAME}/{sha256[:2]}/{sha256}.jpg"


def resolve_shot_path(artifacts_dir: Path, sha256: str) -> Path:
    """Resolve a screenshot's on-disk path, refusing any escape from the
    artifacts root — same guard as `app.monitor.storage.resolve_blob_path`.
    """
    root = artifacts_dir.resolve()
    resolved = (root / shot_relative_path(sha256)).resolve()
    if not resolved.is_relative_to(root):
        raise PermissionError(f"screenshot path escapes the artifacts root: {sha256}")
    return resolved


def store_shot(artifacts_dir: Path, data: bytes) -> StoredShot:
    """Write `data` under its content address, skipping the write if the
    exact bytes are already on disk."""
    sha256 = sha256_hex(data)
    path = resolve_shot_path(artifacts_dir, sha256)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(".tmp")
        tmp_path.write_bytes(data)
        tmp_path.replace(path)
    return StoredShot(sha256=sha256, size_bytes=len(data), path=shot_relative_path(sha256))


def read_shot(artifacts_dir: Path, sha256: str) -> bytes:
    return resolve_shot_path(artifacts_dir, sha256).read_bytes()


def shot_exists(artifacts_dir: Path, sha256: str) -> bool:
    return resolve_shot_path(artifacts_dir, sha256).exists()
