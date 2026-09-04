"""Content-addressed blob store for the Web Archive (task-59 §1.2/§1.8).

A deliberate near-copy of `app.monitor.storage`'s ~70-line pattern under its
OWN prefix (`webarchive/<sha256[:2]>/<sha256>.gz`) rather than an import of
that module: the two stores must never share a directory whose prune logic
could race over the same files, and the task-59 plan keeps `app/monitor/**`
completely untouched (§1.1). Identical bytes — an HTML page or a
subresource, from any client, any capture — are written to disk exactly
once.
"""

from __future__ import annotations

import gzip
import hashlib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class StoredBlob:
    sha256: str
    size_bytes: int
    gzip_size_bytes: int
    path: str  # relative to the artifacts root


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def blob_relative_path(sha256: str) -> str:
    return f"webarchive/{sha256[:2]}/{sha256}.gz"


def resolve_blob_path(artifacts_dir: Path, sha256: str) -> Path:
    """Resolve a blob's on-disk path, refusing any escape from the artifacts
    root — same guard as `app.monitor.storage.resolve_blob_path`.
    """
    root = artifacts_dir.resolve()
    resolved = (root / blob_relative_path(sha256)).resolve()
    if not resolved.is_relative_to(root):
        raise PermissionError(f"blob path escapes the artifacts root: {sha256}")
    return resolved


def store_blob(artifacts_dir: Path, data: bytes) -> StoredBlob:
    """Write `data` under its content address, skipping the write if the
    exact bytes are already on disk.
    """
    sha256 = sha256_hex(data)
    path = resolve_blob_path(artifacts_dir, sha256)
    compressed = gzip.compress(data, compresslevel=6)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(".tmp")
        tmp_path.write_bytes(compressed)
        tmp_path.replace(path)
    return StoredBlob(
        sha256=sha256,
        size_bytes=len(data),
        gzip_size_bytes=len(compressed),
        path=blob_relative_path(sha256),
    )


def read_blob(artifacts_dir: Path, sha256: str) -> bytes:
    path = resolve_blob_path(artifacts_dir, sha256)
    with gzip.open(path, "rb") as handle:
        return handle.read()


def blob_exists_locally(artifacts_dir: Path, sha256: str) -> bool:
    return resolve_blob_path(artifacts_dir, sha256).exists()


def delete_blob_file(artifacts_dir: Path, sha256: str) -> bool:
    path = resolve_blob_path(artifacts_dir, sha256)
    if path.exists():
        path.unlink()
        return True
    return False
