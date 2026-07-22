"""Незмінний snapshot, вибір latest-success і те, як це видно на дашборді
(PLAN §6, §12, §15, §18).

Перевіряється наскрізь: worker пише snapshot -> репозиторій обирає останній
успішний -> `GET /api/v1/dashboard` показує стани, які **семантично різні**
для `not_configured`, `failed` і зібраних даних.
"""

from __future__ import annotations

import pytest
from harness import FakeScenario, running_worker, wait_for
from sqlalchemy import select

from app.db.models import ClientIntegration, ProviderSnapshot
from app.db.repositories.cells import CellRepository
from app.db.repositories.snapshots import SnapshotRepository
from app.schemas.enums import CellStatus, Provider

pytestmark = pytest.mark.integration

SYNC_URL = "/api/v1/sync-jobs"
PENDING = {CellStatus.queued, CellStatus.running, CellStatus.cancel_requested}


def _post_run(api_client, client_ids, providers, **extra):
    response = api_client.post(
        SYNC_URL,
        json={
            "client_ids": list(client_ids),
            "providers": [p.value for p in providers],
            "period_kind": "week",
            "anchor": "2026-06-15",
            **extra,
        },
    )
    assert response.status_code == 202, response.text
    return response.json()


def _run_until_done(temp_db, job_id, providers=None):
    def _done() -> bool:
        with temp_db.session_scope() as session:
            return not any(
                c.status in PENDING for c in CellRepository(session).list_for_job(job_id)
            )

    with running_worker(temp_db.session_scope, providers=providers):
        wait_for(_done, timeout=30, what="job завершився")


def _snapshots(temp_db):
    with temp_db.session_scope() as session:
        return [
            (s.id, s.provider, s.observed_at, s.quality, tuple(s.warnings_json or []))
            for s in session.scalars(select(ProviderSnapshot).order_by(ProviderSnapshot.created_at))
        ]


def test_force_refresh_adds_a_snapshot_and_never_rewrites_the_old_one(
    temp_db, api_client, fake_adapters, seed_client
):
    client_id = seed_client(scenarios={Provider.ahrefs: FakeScenario()})

    first_job = _post_run(api_client, [client_id], [Provider.ahrefs])["job_id"]
    _run_until_done(temp_db, first_job, providers=(Provider.ahrefs,))
    after_first = _snapshots(temp_db)
    assert len(after_first) == 1

    second_job = _post_run(
        api_client, [client_id], [Provider.ahrefs], mode="force_refresh"
    )["job_id"]
    _run_until_done(temp_db, second_job, providers=(Provider.ahrefs,))
    after_second = _snapshots(temp_db)

    assert len(after_second) == 2, "force_refresh має додати новий рядок, а не оновити старий"
    assert after_second[0] == after_first[0], "перший snapshot незмінний після другого збору"

    with temp_db.session_scope() as session:
        latest = SnapshotRepository(session).latest_success(client_id, Provider.ahrefs)
        assert latest is not None
        assert latest.id == after_second[1][0], "latest_success має вказувати на найновіший збір"


def test_second_run_without_force_refresh_skips_a_fresh_cell(
    temp_db, api_client, fake_adapters, seed_client
):
    """`collect_missing` не витрачає units на те, що вже свіже (PLAN §9.3)."""
    client_id = seed_client(scenarios={Provider.semrush: FakeScenario()})

    first_job = _post_run(api_client, [client_id], [Provider.semrush])["job_id"]
    _run_until_done(temp_db, first_job, providers=(Provider.semrush,))

    second_job = _post_run(api_client, [client_id], [Provider.semrush])["job_id"]
    _run_until_done(temp_db, second_job, providers=(Provider.semrush,))

    with temp_db.session_scope() as session:
        cells = CellRepository(session).list_for_job(second_job)
    assert [c.status for c in cells] == [CellStatus.skipped]
    assert len(_snapshots(temp_db)) == 1


def test_dashboard_distinguishes_not_configured_failed_and_collected(
    temp_db, api_client, fake_adapters, seed_client
):
    client_id = seed_client(
        name="Acme Dental",
        scenarios={
            Provider.ahrefs: FakeScenario(),
            Provider.semrush: FakeScenario(scenario="fail"),
            Provider.gsc: FakeScenario(scenario="unavailable"),
            # reporting_ninja навмисно без інтеграції -> not_configured
        },
    )
    job_id = _post_run(
        api_client, [client_id], [Provider.ahrefs, Provider.semrush, Provider.gsc]
    )["job_id"]
    _run_until_done(temp_db, job_id)

    body = api_client.get("/api/v1/dashboard", params={"period": "week"}).json()
    assert len(body["clients"]) == 1
    card = body["clients"][0]
    states = {summary["provider"]: summary["state"] for summary in card["providers"]}

    assert states[Provider.ahrefs.value] == "complete"
    assert states[Provider.semrush.value] == "failed"
    assert states[Provider.gsc.value] == "unavailable"
    assert states[Provider.reporting_ninja.value] == "not_configured"
    assert len({states[p] for p in states}) == 4, "чотири стани мають лишатись різними"

    ninja = next(s for s in card["providers"] if s["provider"] == Provider.reporting_ninja.value)
    assert ninja["metrics"] == [], "не налаштований сервіс не показує метрик — і точно не нулі"
    assert ninja["last_success_at"] is None

    assert body["current_range"]["start"] < body["current_range"]["end"]
    assert body["previous_range"]["end"] < body["current_range"]["start"]


def test_disabled_integration_is_not_configured_not_a_failure(
    temp_db, api_client, fake_adapters, seed_client
):
    client_id = seed_client(scenarios={Provider.ahrefs: FakeScenario()})
    with temp_db.session_scope() as session:
        integration = session.scalars(
            select(ClientIntegration).where(ClientIntegration.client_id == client_id)
        ).one()
        integration.enabled = False

    job_id = _post_run(api_client, [client_id], [Provider.ahrefs])["job_id"]
    _run_until_done(temp_db, job_id, providers=(Provider.ahrefs,))

    with temp_db.session_scope() as session:
        cells = CellRepository(session).list_for_job(job_id)
    assert [c.status for c in cells] == [CellStatus.not_configured]
    assert _snapshots(temp_db) == []
