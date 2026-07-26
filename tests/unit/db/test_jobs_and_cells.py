"""JobRepository/CellRepository: decomposition into cells, atomic claim, aggregate status."""

from __future__ import annotations

from datetime import date

from app.db.base import utcnow
from app.db.models import Client
from app.db.repositories.cells import CellRepository
from app.db.repositories.jobs import JobRepository
from app.schemas.enums import CellStatus, ComparisonMode, JobStatus, PeriodKind, Provider


def _make_client(session, slug="acme") -> Client:
    client = Client(name="Acme", slug=slug, domain=f"{slug}.com")
    session.add(client)
    session.commit()
    return client


def test_create_run_decomposes_into_cells(session):
    client = _make_client(session)
    jobs = JobRepository(session).create_run(
        client_ids=[client.id],
        providers=[Provider.ahrefs, Provider.semrush],
        period_kind=PeriodKind.week,
        comparison_mode=ComparisonMode.closed,
        anchor_date=date(2026, 1, 1),
    )
    assert len(jobs) == 1
    job = jobs[0]

    cells = CellRepository(session)
    ahrefs_cell = cells.claim_next_cell(Provider.ahrefs, lease_owner="worker-1")
    semrush_cell = cells.claim_next_cell(Provider.semrush, lease_owner="worker-1")

    assert ahrefs_cell is not None and ahrefs_cell.job_id == job.id
    assert semrush_cell is not None and semrush_cell.job_id == job.id
    assert ahrefs_cell.status == CellStatus.running


def test_create_run_persists_custom_range(session):
    client = _make_client(session)

    job = JobRepository(session).create_run(
        client_ids=[client.id],
        providers=[Provider.gsc],
        period_kind=PeriodKind.custom,
        comparison_mode=ComparisonMode.closed,
        anchor_date=date(2026, 1, 1),
        custom_start=date(2025, 3, 1),
        custom_end=date(2025, 3, 31),
    )[0]

    assert job.custom_start == date(2025, 3, 1)
    assert job.custom_end == date(2025, 3, 31)


def test_cell_terminal_progress_is_complete_and_null_updates_do_not_reset_it(session):
    client = _make_client(session)
    job = JobRepository(session).create_run(
        client_ids=[client.id],
        providers=[Provider.gsc],
        period_kind=PeriodKind.week,
        comparison_mode=ComparisonMode.closed,
        anchor_date=date(2026, 1, 1),
    )[0]
    cell = CellRepository(session).list_for_job(job.id)[0]
    cells = CellRepository(session)

    cells.set_progress(cell.id, 44.44, current_stage="fetch_current_query")
    cells.set_progress(cell.id, None, current_stage="fetch_previous_query")
    assert cells.get(cell.id).progress_percent == 44.44

    cells.set_status(cell.id, CellStatus.succeeded)
    completed = cells.get(cell.id)
    assert completed.progress_percent == 100.0
    assert completed.current_stage == "complete"


def test_cell_progress_message_is_set_and_null_updates_do_not_reset_it(session):
    client = _make_client(session)
    job = JobRepository(session).create_run(
        client_ids=[client.id],
        providers=[Provider.gsc],
        period_kind=PeriodKind.week,
        comparison_mode=ComparisonMode.closed,
        anchor_date=date(2026, 1, 1),
    )[0]
    cell = CellRepository(session).list_for_job(job.id)[0]
    cells = CellRepository(session)

    assert cells.get(cell.id).progress_message is None

    cells.set_progress(cell.id, 10.0, progress_message="found 100 posts so far")
    assert cells.get(cell.id).progress_message == "found 100 posts so far"

    cells.set_progress(cell.id, 20.0, progress_message="found 148 posts so far")
    assert cells.get(cell.id).progress_message == "found 148 posts so far"

    # An event without a message must not blank out the last real one.
    cells.set_progress(cell.id, 30.0, current_stage="persist")
    assert cells.get(cell.id).progress_message == "found 148 posts so far"


def test_claim_next_cell_is_atomic_across_two_callers(session, engine):
    from sqlalchemy.orm import sessionmaker

    client = _make_client(session)
    JobRepository(session).create_run(
        client_ids=[client.id],
        providers=[Provider.ahrefs],
        period_kind=PeriodKind.week,
        comparison_mode=ComparisonMode.closed,
        anchor_date=date(2026, 1, 1),
    )
    session.commit()

    maker = sessionmaker(bind=engine, expire_on_commit=False)
    session_a = maker()
    session_b = maker()
    try:
        cell_a = CellRepository(session_a).claim_next_cell(Provider.ahrefs, lease_owner="a")
        session_a.commit()
        cell_b = CellRepository(session_b).claim_next_cell(Provider.ahrefs, lease_owner="b")
        session_b.commit()

        assert cell_a is not None
        assert cell_b is None
    finally:
        session_a.close()
        session_b.close()


def test_expired_lease_is_reclaimable(session):
    client = _make_client(session)
    JobRepository(session).create_run(
        client_ids=[client.id],
        providers=[Provider.ahrefs],
        period_kind=PeriodKind.week,
        comparison_mode=ComparisonMode.closed,
        anchor_date=date(2026, 1, 1),
    )
    cells = CellRepository(session)
    cell = cells.claim_next_cell(Provider.ahrefs, lease_owner="worker-1", lease_ttl_seconds=-10)
    session.commit()

    expired = cells.expired_cells(now=utcnow())
    assert [c.id for c in expired] == [cell.id]


def test_profile_lock_blocks_concurrent_claim_same_profile(session):
    client_a = _make_client(session, slug="a")
    client_b = _make_client(session, slug="b")
    from app.db.models import ClientIntegration

    session.add_all(
        [
            ClientIntegration(
                client_id=client_a.id,
                provider=Provider.semrush,
                config_json={"profile_name": "shared"},
            ),
            ClientIntegration(
                client_id=client_b.id,
                provider=Provider.semrush,
                config_json={"profile_name": "shared"},
            ),
        ]
    )
    session.commit()

    JobRepository(session).create_run(
        client_ids=[client_a.id, client_b.id],
        providers=[Provider.semrush],
        period_kind=PeriodKind.week,
        comparison_mode=ComparisonMode.closed,
        anchor_date=date(2026, 1, 1),
    )

    cells = CellRepository(session)
    first = cells.claim_next_cell(Provider.semrush, lease_owner="worker-1")
    session.commit()
    assert first is not None

    second = cells.claim_next_cell(Provider.semrush, lease_owner="worker-1")
    assert second is None  # same profile_key is held by `first`, still running


def test_profile_lock_blocks_different_providers_sharing_physical_store(session):
    client = _make_client(session, slug="shared-store")
    from app.db.models import ClientIntegration

    session.add_all(
        [
            ClientIntegration(
                client_id=client.id,
                provider=Provider.semrush,
                config_json={"profile": "semrush", "profile_store": "semrush"},
            ),
            ClientIntegration(
                client_id=client.id,
                provider=Provider.gsc,
                config_json={"profile": "gsc", "profile_store": "semrush"},
            ),
        ]
    )
    session.commit()

    JobRepository(session).create_run(
        client_ids=[client.id],
        providers=[Provider.semrush, Provider.gsc],
        period_kind=PeriodKind.week,
        comparison_mode=ComparisonMode.closed,
        anchor_date=date(2026, 1, 1),
    )

    cells = CellRepository(session)
    assert cells.claim_next_cell(Provider.semrush, lease_owner="worker-1") is not None
    session.commit()
    assert cells.claim_next_cell(Provider.gsc, lease_owner="worker-1") is None


def test_aggregate_status_ignores_not_configured_and_skipped(session):
    client = _make_client(session)
    jobs = JobRepository(session).create_run(
        client_ids=[client.id],
        providers=[Provider.ahrefs, Provider.semrush, Provider.gsc],
        period_kind=PeriodKind.week,
        comparison_mode=ComparisonMode.closed,
        anchor_date=date(2026, 1, 1),
    )
    job = jobs[0]
    cells = CellRepository(session)

    for provider in (Provider.ahrefs, Provider.semrush, Provider.gsc):
        cells.claim_next_cell(provider, lease_owner="worker-1")

    from sqlalchemy import select

    from app.db.models import SyncJobProvider

    rows = list(
        session.scalars(select(SyncJobProvider).where(SyncJobProvider.job_id == job.id))
    )
    by_provider = {row.provider: row for row in rows}
    cells.set_status(by_provider[Provider.ahrefs].id, CellStatus.succeeded)
    cells.set_status(by_provider[Provider.semrush].id, CellStatus.not_configured)
    cells.set_status(by_provider[Provider.gsc].id, CellStatus.skipped)

    repo = JobRepository(session)
    assert repo.aggregate_status(job_id=job.id) == JobStatus.succeeded


def test_aggregate_status_is_partial_when_attempts_mixed(session):
    client = _make_client(session)
    jobs = JobRepository(session).create_run(
        client_ids=[client.id],
        providers=[Provider.ahrefs, Provider.semrush],
        period_kind=PeriodKind.week,
        comparison_mode=ComparisonMode.closed,
        anchor_date=date(2026, 1, 1),
    )
    job = jobs[0]
    cells = CellRepository(session)
    for provider in (Provider.ahrefs, Provider.semrush):
        cells.claim_next_cell(provider, lease_owner="worker-1")

    from sqlalchemy import select

    from app.db.models import SyncJobProvider

    rows = list(
        session.scalars(select(SyncJobProvider).where(SyncJobProvider.job_id == job.id))
    )
    by_provider = {row.provider: row for row in rows}
    cells.set_status(by_provider[Provider.ahrefs].id, CellStatus.succeeded)
    cells.set_status(by_provider[Provider.semrush].id, CellStatus.failed, error_code="timeout")

    assert JobRepository(session).aggregate_status(job_id=job.id) == JobStatus.partial
