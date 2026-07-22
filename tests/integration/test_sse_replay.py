"""SSE-прогрес і replay через `Last-Event-ID` (PLAN §9.3, §15).

Тест ганяє справжній стрім із `GET /sync-jobs/{id}/events`, а не
`event_stream()` напряму: перевіряти треба саме зв'язку роутер + репозиторій
подій + формат SSE, бо reconnect фронту (task-12) спирається на `id:` рядок,
який формується тут.
"""

from __future__ import annotations

import json

import pytest
from harness import FakeScenario, running_worker, wait_for

from app.db.repositories.cells import CellRepository
from app.schemas.enums import CellStatus, Provider

pytestmark = pytest.mark.integration

SYNC_URL = "/api/v1/sync-jobs"


def _run_to_completion(temp_db, api_client, client_id, providers):
    response = api_client.post(
        SYNC_URL,
        json={
            "client_ids": [client_id],
            "providers": [p.value for p in providers],
            "period_kind": "week",
            "anchor": "2026-06-15",
        },
    )
    assert response.status_code == 202, response.text
    job_id = response.json()["job_id"]

    pending = {CellStatus.queued, CellStatus.running, CellStatus.cancel_requested}

    def _done() -> bool:
        with temp_db.session_scope() as session:
            return not any(
                c.status in pending for c in CellRepository(session).list_for_job(job_id)
            )

    with running_worker(temp_db.session_scope):
        wait_for(_done, timeout=30, what="job завершився")
    return job_id


def _read_stream(api_client, job_id, *, last_event_id=None):
    headers = {"Last-Event-ID": str(last_event_id)} if last_event_id is not None else {}
    with api_client.stream("GET", f"{SYNC_URL}/{job_id}/events", headers=headers) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        body = b"".join(response.iter_bytes()).decode("utf-8")

    events = []
    for block in body.split("\n\n"):
        lines = [line for line in block.splitlines() if line and not line.startswith(":")]
        if not lines:
            continue
        seq = int(next(line for line in lines if line.startswith("id: "))[4:])
        payload = json.loads(next(line for line in lines if line.startswith("data: "))[6:])
        events.append((seq, payload))
    return events


def test_stream_replays_only_events_after_last_event_id(
    temp_db, api_client, fake_adapters, seed_client
):
    client_id = seed_client(scenarios={Provider.ahrefs: FakeScenario()})
    job_id = _run_to_completion(temp_db, api_client, client_id, [Provider.ahrefs])

    full = _read_stream(api_client, job_id)
    assert len(full) >= 4, "адаптер емітить подію на кожну стадію"
    seqs = [seq for seq, _ in full]
    assert seqs == sorted(seqs), "seq має бути монотонним"
    assert len(set(seqs)) == len(seqs), "seq не має повторюватись"
    assert all(payload["provider"] == "ahrefs" for _, payload in full)
    assert all(payload["percent"] is not None for _, payload in full)

    cut = seqs[len(seqs) // 2]
    replayed = _read_stream(api_client, job_id, last_event_id=cut)

    assert [seq for seq, _ in replayed] == [seq for seq in seqs if seq > cut], (
        "reconnect має віддати рівно хвіст після Last-Event-ID — без пропусків і без повторів"
    )
    assert dict(full)[seqs[-1]] == dict(replayed)[seqs[-1]], (
        "повторно віддана подія має бути тією ж"
    )


def test_stream_for_unknown_id_is_404(api_client, fake_adapters):
    response = api_client.get(f"{SYNC_URL}/00000000-0000-0000-0000-000000000000/events")
    assert response.status_code == 404
    assert response.json()["detail"]["error_code"] == "job_not_found"


def test_batch_stream_merges_events_of_all_its_jobs(
    temp_db, api_client, fake_adapters, seed_client
):
    first = seed_client(
        slug="alpha", domain="alpha.example.com", scenarios={Provider.gsc: FakeScenario()}
    )
    second = seed_client(
        slug="beta", domain="beta.example.com", scenarios={Provider.gsc: FakeScenario()}
    )
    response = api_client.post(
        SYNC_URL,
        json={
            "client_ids": [first, second],
            "providers": ["gsc"],
            "period_kind": "week",
            "anchor": "2026-06-15",
        },
    )
    batch_id = response.json()["batch_id"]
    assert batch_id is not None

    pending = {CellStatus.queued, CellStatus.running}

    def _done() -> bool:
        with temp_db.session_scope() as session:
            return not any(
                c.status in pending for c in CellRepository(session).list_for_batch(batch_id)
            )

    with running_worker(temp_db.session_scope, providers=(Provider.gsc,)):
        wait_for(_done, timeout=30, what="batch завершився")

    events = _read_stream(api_client, batch_id)
    stages = {payload["stage"] for _, payload in events}
    assert {"authenticate", "fetch_current", "fetch_previous", "normalize"} <= stages
    assert len(events) >= 8, "у стрімі батча мають бути події обох клієнтів"
