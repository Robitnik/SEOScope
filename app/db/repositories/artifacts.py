"""Artifact repository (`artifacts`, PLAN §6/§13): raw diagnostic files' metadata.

Actual file bytes live on the artifacts volume, addressed by a path relative
to `Settings.artifacts_dir`; this repository only ever hands back a path
that has been verified to resolve inside that root (`safe_resolve`).
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy.orm import Session

from app.core.security import safe_resolve
from app.db.models import Artifact
from app.schemas.enums import Provider


class ArtifactRepository:
    def __init__(self, session: Session, artifacts_root: Path) -> None:
        self._session = session
        self._artifacts_root = artifacts_root

    def create(
        self,
        *,
        job_id: str,
        provider: Provider,
        kind: str,
        path: str,
        sha256: str,
        size_bytes: int | None = None,
        mime: str | None = None,
    ) -> Artifact:
        # Raises PathTraversalError immediately if `path` would escape the root —
        # fail at write time, not at download time.
        safe_resolve(self._artifacts_root, path)
        artifact = Artifact(
            job_id=job_id,
            provider=provider,
            kind=kind,
            path=path,
            sha256=sha256,
            size_bytes=size_bytes,
            mime=mime,
        )
        self._session.add(artifact)
        self._session.flush()
        return artifact

    def get_for_download(self, artifact_id: str) -> tuple[Artifact, Path] | None:
        artifact = self._session.get(Artifact, artifact_id)
        if artifact is None:
            return None
        resolved = safe_resolve(self._artifacts_root, artifact.path)
        return artifact, resolved
