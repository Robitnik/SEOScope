from __future__ import annotations

from app.db.repositories.cells import CellRepository
from app.db.repositories.clients import ClientRepository
from app.db.repositories.events import EventRepository
from app.db.repositories.jobs import JobRepository
from app.schemas.enums import CellStatus, Provider, SourceMode


def _seed_client(db, *, slug: str = "acme") -> str:
    clients = ClientRepository(db)
    c = clients.create(name="Acme", slug=slug, domain=f"{slug}.com", timezone="America/New_York")
    clients.upsert_integration(c.id, Provider.gsc, enabled=True, source_mode=SourceMode.api)
    db.commit()
    return c.id


def _base_request(client_ids, providers) -> dict:
    return {
        "client_ids": client_ids,
        "providers": providers,
        "period_kind": "week",
        "mode": "collect_missing",
    }


def test_create_single_client_job_returns_job_id(client, db):
    client_id = _seed_client(db)
    response = client.post(
        "/api/v1/sync-jobs", json=_base_request([client_id], [Provider.gsc.value])
    )
    assert response.status_code == 202
    body = response.json()
    assert body["job_id"] is not None
    assert body["batch_id"] is None
    assert body["status_url"] == f"/api/v1/sync-jobs/{body['job_id']}"
    assert body["events_url"].endswith("/events")


def test_create_batch_job_returns_batch_id(client, db):
    a = _seed_client(db, slug="acme")
    b = _seed_client(db, slug="beta")
    response = client.post("/api/v1/sync-jobs", json=_base_request([a, b], [Provider.gsc.value]))
    assert response.status_code == 202
    body = response.json()
    assert body["job_id"] is None
    assert body["batch_id"] is not None


def test_create_job_unknown_client_is_404(client):
    response = client.post("/api/v1/sync-jobs", json=_base_request(["nope"], [Provider.gsc.value]))
    assert response.status_code == 404
    assert response.json()["detail"]["error_code"] == "client_not_found"


def test_get_job_returns_per_cell_progress(client, db):
    client_id = _seed_client(db)
    created = client.post(
        "/api/v1/sync-jobs", json=_base_request([client_id], [Provider.gsc.value])
    ).json()
    job_id = created["job_id"]

    response = client.get(f"/api/v1/sync-jobs/{job_id}")
    assert response.status_code == 200
    body = response.json()
    assert body["client_id"] == client_id
    assert len(body["cells"]) == 1
    assert body["cells"][0]["provider"] == Provider.gsc.value
    assert body["cells"][0]["status"] == "queued"


def test_get_batch_returns_batch_progress(client, db):
    a = _seed_client(db, slug="acme")
    b = _seed_client(db, slug="beta")
    created = client.post(
        "/api/v1/sync-jobs", json=_base_request([a, b], [Provider.gsc.value])
    ).json()
    batch_id = created["batch_id"]

    response = client.get(f"/api/v1/sync-jobs/{batch_id}")
    assert response.status_code == 200
    body = response.json()
    assert body["batch_id"] == batch_id
    assert len(body["cells"]) == 2


def test_get_unknown_job_is_404(client):
    response = client.get("/api/v1/sync-jobs/nope")
    assert response.status_code == 404


def test_list_jobs_filters_by_client_id(client, db):
    a = _seed_client(db, slug="acme")
    b = _seed_client(db, slug="beta")
    client.post("/api/v1/sync-jobs", json=_base_request([a], [Provider.gsc.value]))
    client.post("/api/v1/sync-jobs", json=_base_request([b], [Provider.gsc.value]))

    response = client.get("/api/v1/sync-jobs", params={"client_id": a})
    body = response.json()
    assert len(body) == 1
    assert body[0]["client_id"] == a


def test_cancel_job_flags_cancel_requested(client, db):
    client_id = _seed_client(db)
    created = client.post(
        "/api/v1/sync-jobs", json=_base_request([client_id], [Provider.gsc.value])
    ).json()
    job_id = created["job_id"]

    response = client.post(f"/api/v1/sync-jobs/{job_id}/cancel")
    assert response.status_code == 200
    assert response.json()["status"] == "cancel_requested"


def test_cancel_unknown_job_is_404(client):
    response = client.post("/api/v1/sync-jobs/nope/cancel")
    assert response.status_code == 404


def test_retry_requeues_only_failed_cells(client, db):
    client_id = _seed_client(db)
    created = client.post(
        "/api/v1/sync-jobs", json=_base_request([client_id], [Provider.gsc.value])
    ).json()
    job_id = created["job_id"]

    cell = CellRepository(db).list_for_job(job_id)[0]
    CellRepository(db).set_status(cell.id, CellStatus.failed, error_code="boom")
    JobRepository(db).sync_status_from_cells(job_id)
    db.commit()

    response = client.post(f"/api/v1/sync-jobs/{job_id}/retry")
    assert response.status_code == 200
    body = response.json()
    assert body["cells"][0]["status"] == "queued"
    assert body["cells"][0]["attempt"] == 2
    assert body["cells"][0]["error_code"] is None


def test_events_stream_replays_and_terminates_when_job_is_done(client, db):
    client_id = _seed_client(db)
    created = client.post(
        "/api/v1/sync-jobs", json=_base_request([client_id], [Provider.gsc.value])
    ).json()
    job_id = created["job_id"]

    EventRepository(db).append(job_id, "info", provider=Provider.gsc, stage="collect", message="hi")
    cell = CellRepository(db).list_for_job(job_id)[0]
    CellRepository(db).set_status(cell.id, CellStatus.succeeded)
    JobRepository(db).sync_status_from_cells(job_id)
    db.commit()

    response = client.get(f"/api/v1/sync-jobs/{job_id}/events")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "data:" in response.text
    assert '"message": "hi"' in response.text
    assert "id: 1" in response.text


def test_events_stream_no_secrets_in_events(client, db):
    """`make_emitter`'s redaction (task-05) is the actual guard; this only
    confirms the SSE endpoint serves whatever was persisted without
    re-exposing anything the emitter already redacted.
    """
    from app.db.session import worker_session_scope
    from app.jobs.events import make_emitter
    from app.schemas.adapter import ProgressEvent

    client_id = _seed_client(db)
    created = client.post(
        "/api/v1/sync-jobs", json=_base_request([client_id], [Provider.gsc.value])
    ).json()
    job_id = created["job_id"]
    cell = CellRepository(db).list_for_job(job_id)[0]

    emit = make_emitter(worker_session_scope, job_id=job_id, cell_id=cell.id, lease_owner="test")
    emit(
        ProgressEvent(
            provider=Provider.gsc,
            stage="authenticate",
            message="Authorization: Bearer sekrit-token-value",
        )
    )

    CellRepository(db).set_status(cell.id, CellStatus.succeeded)
    JobRepository(db).sync_status_from_cells(job_id)
    db.commit()

    response = client.get(f"/api/v1/sync-jobs/{job_id}/events")
    assert "sekrit-token-value" not in response.text
    assert "REDACTED" in response.text


def test_artifact_download_roundtrip(client, db):
    from app.core.config import get_settings
    from app.db.repositories.artifacts import ArtifactRepository

    client_id = _seed_client(db)
    created = client.post(
        "/api/v1/sync-jobs", json=_base_request([client_id], [Provider.gsc.value])
    ).json()
    job_id = created["job_id"]

    settings = get_settings()
    settings.artifacts_dir.mkdir(parents=True, exist_ok=True)
    (settings.artifacts_dir / "raw.json").write_bytes(b'{"ok": true}')
    artifact = ArtifactRepository(db, settings.artifacts_dir).create(
        job_id=job_id,
        provider=Provider.gsc,
        kind="raw_response",
        path="raw.json",
        sha256="deadbeef",
        mime="application/json",
    )
    db.commit()

    response = client.get(f"/api/v1/artifacts/{artifact.id}")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert response.json() == {"ok": True}


def test_artifact_download_missing_is_404(client):
    response = client.get("/api/v1/artifacts/nope")
    assert response.status_code == 404
    assert response.json()["detail"]["error_code"] == "artifact_not_found"
