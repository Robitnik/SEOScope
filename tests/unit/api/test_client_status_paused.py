"""Що саме змінює пауза клієнта (task-34).

Одна ідея на весь файл: **«all» ніколи не включає паузованого, явний вибір —
включає**. Тому кожен тест перевіряє пару «масова операція / явний список», а
не лише щасливий шлях.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from app.db.repositories.analytics_datasets import (
    AnalyticsDatasetRepository,
    AnalyticsDatasetRowInput,
)
from app.db.repositories.clients import ClientRepository
from app.db.repositories.jobs import JobRepository
from app.db.repositories.snapshots import SnapshotRepository
from app.schemas.enums import (
    ClientStatus,
    ComparisonMode,
    PeriodKind,
    Provider,
    Quality,
    SourceMode,
)


def _client_row(db, *, slug: str, paused: bool = False):
    repo = ClientRepository(db)
    created = repo.create(
        name=slug.title(), slug=slug, domain=f"{slug}.com", timezone="America/New_York"
    )
    repo.upsert_integration(created.id, Provider.gsc, enabled=True, source_mode=SourceMode.api)
    if paused:
        repo.set_status(created.id, ClientStatus.paused, note="payment stopped")
    db.commit()
    return created


def _queued_client_ids(db, body) -> set[str]:
    """Кого реально поставили в чергу — незалежно від того, вийшов батч чи
    одинична джоба (одна клітинка `batch_id` не отримує)."""
    repo = JobRepository(db)
    if body.get("batch_id"):
        return {job.client_id for job in repo.list_for_batch(body["batch_id"])}
    return {repo.get(body["job_id"]).client_id}


def _sync_request(client_ids, providers=("gsc",)) -> dict:
    return {
        "client_ids": client_ids,
        "providers": list(providers),
        "period_kind": "week",
        "mode": "collect_missing",
    }


# --- sync jobs --------------------------------------------------------------


def test_sync_all_skips_paused_and_reports_them(client, db):
    active = _client_row(db, slug="active-co")
    paused = _client_row(db, slug="paused-co", paused=True)

    response = client.post("/api/v1/sync-jobs", json=_sync_request("all"))

    assert response.status_code == 202
    body = response.json()
    assert body["skipped_paused_client_ids"] == [paused.id]
    assert _queued_client_ids(db, body) == {active.id}


def test_sync_of_an_explicitly_named_paused_client_still_runs(client, db):
    paused = _client_row(db, slug="paused-co", paused=True)

    response = client.post("/api/v1/sync-jobs", json=_sync_request([paused.id]))

    assert response.status_code == 202
    assert response.json()["skipped_paused_client_ids"] == []
    assert JobRepository(db).get(response.json()["job_id"]).client_id == paused.id


def test_sync_all_when_every_client_is_paused_says_so(client, db):
    paused = _client_row(db, slug="paused-co", paused=True)

    response = client.post("/api/v1/sync-jobs", json=_sync_request("all"))

    assert response.status_code == 400
    detail = response.json()["detail"]
    assert detail["error_code"] == "all_clients_paused"
    assert detail["details"]["client_ids"] == [paused.id]


# --- technical audits -------------------------------------------------------


def test_audit_batch_all_skips_paused(client, db):
    active = _client_row(db, slug="audit-active")
    paused = _client_row(db, slug="audit-paused", paused=True)

    response = client.post("/api/v1/tech-audit/runs", json={"client_ids": "all"})

    assert response.status_code == 202
    body = response.json()
    assert body["queued_count"] == 1
    assert body["skipped_paused_client_ids"] == [paused.id]
    assert _queued_client_ids(db, body) == {active.id}


def test_audit_batch_runs_a_paused_client_when_named(client, db):
    paused = _client_row(db, slug="audit-paused", paused=True)

    response = client.post("/api/v1/tech-audit/runs", json={"client_ids": [paused.id]})

    assert response.status_code == 202
    assert response.json()["queued_count"] == 1


def test_audit_batch_all_paused_is_409_with_its_own_code(client, db):
    _client_row(db, slug="audit-paused", paused=True)

    response = client.post("/api/v1/tech-audit/runs", json={"client_ids": "all"})

    assert response.status_code == 409
    assert response.json()["detail"]["error_code"] == "all_clients_paused"


# --- dashboard --------------------------------------------------------------


def test_dashboard_shows_paused_clients_by_default_with_their_status(client, db):
    _client_row(db, slug="active-co")
    paused = _client_row(db, slug="paused-co", paused=True)

    body = client.get("/api/v1/dashboard").json()

    by_id = {card["client_id"]: card for card in body["clients"]}
    assert paused.id in by_id
    assert by_id[paused.id]["status"] == "paused"
    assert by_id[paused.id]["status_note"] == "payment stopped"
    assert by_id[paused.id]["status_changed_at"] is not None


def test_dashboard_client_status_filter(client, db):
    active = _client_row(db, slug="active-co")
    paused = _client_row(db, slug="paused-co", paused=True)

    only_active = client.get("/api/v1/dashboard?client_status=active").json()
    only_paused = client.get("/api/v1/dashboard?client_status=paused").json()

    assert [c["client_id"] for c in only_active["clients"]] == [active.id]
    assert [c["client_id"] for c in only_paused["clients"]] == [paused.id]


def test_dashboard_puts_paused_clients_last_in_every_sort_mode(client, db):
    # Назва паузованого клієнта навмисно перша за алфавітом: якби статус не
    # відтісняв його вниз, у режимі `name` він стояв би зверху.
    paused = _client_row(db, slug="aaa-paused", paused=True)
    active = _client_row(db, slug="zzz-active")

    for mode in ("quality", "health", "name", "last_sync", "biggest_drop"):
        body = client.get(f"/api/v1/dashboard?sort={mode}").json()
        assert [c["client_id"] for c in body["clients"]] == [active.id, paused.id], mode


# --- portfolio trends -------------------------------------------------------


def _seed_daily(db, created, *, day: date, clicks: int) -> None:
    job = JobRepository(db).create_run(
        client_ids=[created.id],
        providers=[Provider.gsc],
        period_kind=PeriodKind.week,
        comparison_mode=ComparisonMode.closed,
        anchor_date=day,
    )[0]
    snapshot = SnapshotRepository(db).create_immutable(
        job_id=job.id,
        client_id=created.id,
        provider=Provider.gsc,
        source_mode=SourceMode.api,
        observed_at=datetime.now(UTC),
        quality=Quality.complete,
    )
    AnalyticsDatasetRepository(db).bulk_insert(
        snapshot_id=snapshot.id,
        client_id=created.id,
        provider=Provider.gsc,
        period_start=day,
        period_end=day,
        rows=[
            AnalyticsDatasetRowInput(
                dataset_key="gsc_daily",
                dimension_key="date",
                dimension_value=day.isoformat(),
                metrics={"clicks": Decimal(clicks), "impressions": Decimal(clicks * 10)},
                quality=Quality.complete,
                rank=1,
            )
        ],
    )
    db.commit()


def test_portfolio_series_defaults_to_active_clients(client, db):
    day = datetime.now(UTC).date() - timedelta(days=1)
    active = _client_row(db, slug="active-co")
    paused = _client_row(db, slug="paused-co", paused=True)
    _seed_daily(db, active, day=day, clicks=10)
    _seed_daily(db, paused, day=day, clicks=20)

    default = client.get("/api/v1/analytics/portfolio-series?metric=gsc_clicks&days=30").json()
    explicit = client.get(
        f"/api/v1/analytics/portfolio-series?metric=gsc_clicks&days=30&client_ids={paused.id}"
    ).json()

    assert [series["client_id"] for series in default["series"]] == [active.id]
    # Явно названий паузований клієнт віддається як і раніше.
    assert [series["client_id"] for series in explicit["series"]] == [paused.id]
