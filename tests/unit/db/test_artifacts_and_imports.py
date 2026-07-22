"""ArtifactRepository path-traversal guard + ImportRepository idempotency."""

from __future__ import annotations

from datetime import date

import pytest

from app.core.security import PathTraversalError
from app.db.models import Client, SyncJob
from app.db.repositories.artifacts import ArtifactRepository
from app.db.repositories.imports import ImportRepository
from app.schemas.enums import ComparisonMode, PeriodKind, Provider


def _make_job(session) -> SyncJob:
    client = Client(name="Acme", slug="acme", domain="acme.com")
    session.add(client)
    session.commit()
    job = SyncJob(
        client_id=client.id,
        period_kind=PeriodKind.week,
        comparison_mode=ComparisonMode.closed,
        anchor_date=date(2026, 1, 1),
    )
    session.add(job)
    session.commit()
    return job


def test_artifact_create_and_get_for_download(session, tmp_path):
    job = _make_job(session)
    root = tmp_path / "artifacts"
    root.mkdir()
    (root / "ahrefs").mkdir()
    (root / "ahrefs" / "raw.json").write_text("{}")

    repo = ArtifactRepository(session, root)
    artifact = repo.create(
        job_id=job.id,
        provider=Provider.ahrefs,
        kind="json",
        path="ahrefs/raw.json",
        sha256="a" * 64,
    )

    found = repo.get_for_download(artifact.id)
    assert found is not None
    fetched, resolved = found
    assert fetched.id == artifact.id
    assert resolved == (root / "ahrefs" / "raw.json").resolve()


def test_artifact_rejects_path_traversal(session, tmp_path):
    job = _make_job(session)
    root = tmp_path / "artifacts"
    root.mkdir()

    repo = ArtifactRepository(session, root)
    with pytest.raises(PathTraversalError):
        repo.create(
            job_id=job.id,
            provider=Provider.ahrefs,
            kind="json",
            path="../../etc/passwd",
            sha256="a" * 64,
        )


def test_import_repository_idempotency(session):
    repo = ImportRepository(session)
    assert repo.seen("deadbeef") is False

    repo.record("clients.json", "deadbeef", importer_version="1.0")

    assert repo.seen("deadbeef") is True
