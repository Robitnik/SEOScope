"""In-memory double for `app.cloud.client.CloudStore` — no network, ever.
Every `app/cloud/**` function that takes a `CloudStore` only calls methods
this class also implements, so tests exercise the exact same call shape the
real B2-backed client uses (verified live, task-52 §1.7).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from app.cloud.client import CloudObjectRef


class FakeCloudStore:
    def __init__(self, *, enabled: bool = True) -> None:
        self.enabled = enabled
        self.objects: dict[str, bytes] = {}
        self.uploads: list[str] = []
        self.fail_keys: set[str] = set()

    def put_bytes(
        self, data: bytes, key: str, *, content_type: str | None = None
    ) -> CloudObjectRef:
        if key in self.fail_keys:
            raise RuntimeError(f"simulated upload failure: {key}")
        self.objects[key] = data
        self.uploads.append(key)
        return CloudObjectRef(key=key, size_bytes=len(data))

    def put_file(
        self, path: Path, key: str, *, content_type: str | None = None
    ) -> CloudObjectRef:
        return self.put_bytes(Path(path).read_bytes(), key, content_type=content_type)

    def get_bytes(self, key: str) -> bytes:
        return self.objects[key]

    def get_to_file(self, key: str, dest: Path) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(self.objects[key])

    def exists(self, key: str) -> CloudObjectRef | None:
        data = self.objects.get(key)
        return None if data is None else CloudObjectRef(key=key, size_bytes=len(data))

    def copy(self, src_key: str, dest_key: str) -> CloudObjectRef:
        data = self.objects[src_key]
        self.objects[dest_key] = data
        return CloudObjectRef(key=dest_key, size_bytes=len(data))

    def delete(self, key: str) -> None:
        self.objects.pop(key, None)

    def list_prefix(self, prefix: str) -> Iterator[CloudObjectRef]:
        for key, data in self.objects.items():
            if key.startswith(prefix):
                yield CloudObjectRef(key=key, size_bytes=len(data))
