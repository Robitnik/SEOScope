"""Cancel / retry-failed-only / partial run через справжній REST + worker
(PLAN §9.2/§9.3, §15, §18).

Ключова властивість, яку тут доводимо: partial-запуск **зберігає** успішні
snapshot-и, а retry чіпає **лише** провалені клітинки — успішні не
перезбираються (це і гроші, і metered units).
"""

from __future__ import annotations

import pytest
from harness import FakeScenario, running_worker, wait_for
from sqlalchemy import select

from app.db.models import ClientIntegration, ProviderSnapshot
from app.db.repositories.cells import CellRepository
from app.schemas.enums import CellStatus, Provider

pytestmark = pytest.mark.integration

SYNC_URL = "/api/v1/sync-jobs"


def _post_run(api_client, client_id, providers, **extra):
    response = api_client.post(
        SYNC_URL,
        json={
            "client_ids": [client_id],
            "providers": [p.value for p in providers],
            "period_kind": "week",
            "anchor": "2026-06-15",
            **extra,
        },
    )
    assert response.status_code == 202, response.text
    return response.json()


def _cells_by_provider(temp_db, job_id):
    with temp_db.session_scope() as session:
        return {c.provider: c for c in CellRepository(session).list_for_job(job_id)}


def _statuses(temp_db, job_id):
    return {p: c.status for p, c in _cells_by_provider(temp_db, job_id).items()}


def _terminal(temp_db, job_id):
    pending = {CellStatus.queued, CellStatus.running, CellStatus.cancel_requested}
    return lambda: not (set(_statuses(temp_db, job_id).values()) & pending)


def test_partial_run_keeps_successes_and_retry_touches_only_failures(
    temp_db, api_client, fake_adapters, seed_client
):
    client_id = seed_client(
        scenarios={
            Provider.ahrefs: FakeScenario(scenario="fail"),
            Provider.semrush: FakeScenario(),
        }
    )
    accepted = _post_run(api_client, client_id, [Provider.ahrefs, Provider.semrush])
    job_id = accepted["job_id"]

    with running_worker(temp_db.session_scope):
        wait_for(_terminal(temp_db, job_id), timeout=30, what="перший запуск завершився")

    assert _statuses(temp_db, job_id) == {
        Provider.ahrefs: CellStatus.failed,
        Provider.semrush: CellStatus.succeeded,
    }
    with temp_db.session_scope() as session:
        snapshots = list(session.scalars(select(ProviderSnapshot)))
    assert [s.provider for s in snapshots] == [Provider.semrush], (
        "частковий запуск зобов'язаний зберегти успішний snapshot"
    )

    body = api_client.get(f"{SYNC_URL}/{job_id}").json()
    assert body["status"] == "partial"

    # «Полагодили» інтеграцію й перезапустили лише провалене.
    with temp_db.session_scope() as session:
        integration = session.scalars(
            select(ClientIntegration).where(
                ClientIntegration.client_id == client_id,
                ClientIntegration.provider == Provider.ahrefs,
            )
        ).one()
        integration.config_json = FakeScenario().as_config()

    retry = api_client.post(f"{SYNC_URL}/{job_id}/retry")
    assert retry.status_code == 200, retry.text

    cells = _cells_by_provider(temp_db, job_id)
    assert cells[Provider.ahrefs].status == CellStatus.queued
    assert cells[Provider.ahrefs].attempt == 2
    assert cells[Provider.semrush].status == CellStatus.succeeded
    assert cells[Provider.semrush].attempt == 1, "успішну клітинку retry чіпати не має"

    with running_worker(temp_db.session_scope):
        wait_for(_terminal(temp_db, job_id), timeout=30, what="retry завершився")

    assert _statuses(temp_db, job_id) == {
        Provider.ahrefs: CellStatus.succeeded,
        Provider.semrush: CellStatus.succeeded,
    }
    with temp_db.session_scope() as session:
        providers_collected = [s.provider for s in session.scalars(select(ProviderSnapshot))]
    assert providers_collected.count(Provider.semrush) == 1, (
        "успішний провайдер не має збиратись удруге під час retry"
    )
    assert providers_collected.count(Provider.ahrefs) == 1


def test_cancel_stops_an_in_flight_cell(temp_db, api_client, fake_adapters, seed_client):
    client_id = seed_client(
        scenarios={Provider.gsc: FakeScenario(scenario="cancel", stage_seconds=0.2)}
    )
    job_id = _post_run(api_client, client_id, [Provider.gsc])["job_id"]

    with running_worker(temp_db.session_scope, providers=(Provider.gsc,)):
        wait_for(
            lambda: _statuses(temp_db, job_id)[Provider.gsc] == CellStatus.running,
            timeout=30,
            what="клітинка почала збір",
        )
        response = api_client.post(f"{SYNC_URL}/{job_id}/cancel")
        assert response.status_code == 200, response.text
        wait_for(
            lambda: _statuses(temp_db, job_id)[Provider.gsc] == CellStatus.cancelled,
            timeout=30,
            what="клітинку скасовано",
        )

    with temp_db.session_scope() as session:
        assert list(session.scalars(select(ProviderSnapshot))) == [], (
            "скасований збір не має лишати snapshot"
        )


def test_cancel_finalizes_a_queued_cell(temp_db, api_client, fake_adapters, seed_client):
    """Був `xfail(strict)` — знахідка task-13 про клітинку, що застрягала в
    `cancel_requested` назавжди. `cancel_run` тепер сам фіналізує клітинки, за
    якими не стежить жоден раннер, тож job чесно доходить до термінального
    стану, а SSE-стрім закривається.
    """
    client_id = seed_client(scenarios={Provider.gsc: FakeScenario()})
    job_id = _post_run(api_client, client_id, [Provider.gsc])["job_id"]

    api_client.post(f"{SYNC_URL}/{job_id}/cancel")
    assert _statuses(temp_db, job_id)[Provider.gsc] == CellStatus.cancelled


def test_repeated_request_with_idempotency_key_creates_no_second_run(
    temp_db, api_client, fake_adapters, seed_client
):
    client_id = seed_client(scenarios={Provider.ahrefs: FakeScenario()})
    providers = [Provider.ahrefs, Provider.semrush]

    first = _post_run(api_client, client_id, providers, idempotency_key="weekly-2026-W25")
    second = _post_run(api_client, client_id, providers, idempotency_key="weekly-2026-W25")

    assert first["job_id"] == second["job_id"]
    assert len(_cells_by_provider(temp_db, first["job_id"])) == 2, (
        "повторний запит не має дублювати клітинки"
    )

    listed = api_client.get(SYNC_URL, params={"client_id": client_id}).json()
    assert len(listed) == 1, "повторний запит не має створювати другий job"
