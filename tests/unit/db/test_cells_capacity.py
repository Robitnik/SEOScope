"""Profile lock as a counting semaphore (task-31).

`claim_next_cell(profile_capacity=N)` is what allows N cells of one provider to
collect at once on ONE logged-in profile — N windows of the same warm Chrome.
The number it enforces is a hard promise to the provider: exceeding it means
more concurrent sessions from one account than the operator configured.
"""

from __future__ import annotations

import threading
from datetime import date

from app.db.models import Client, ClientIntegration
from app.db.repositories.cells import CellRepository
from app.db.repositories.jobs import JobRepository
from app.schemas.enums import CellStatus, ComparisonMode, PeriodKind, Provider


def _seed(session, count: int, *, profile: str | None = "main") -> list[str]:
    client_ids = []
    for index in range(count):
        client = Client(name=f"C{index}", slug=f"c{index}", domain=f"c{index}.com")
        session.add(client)
        session.flush()
        if profile is not None:
            session.add(
                ClientIntegration(
                    client_id=client.id,
                    provider=Provider.ahrefs,
                    enabled=True,
                    config_json={"profile_store": profile},
                )
            )
        client_ids.append(client.id)
    session.flush()
    JobRepository(session).create_run(
        client_ids=client_ids,
        providers=[Provider.ahrefs],
        period_kind=PeriodKind.week,
        comparison_mode=ComparisonMode.closed,
        anchor_date=date(2026, 7, 1),
    )
    session.commit()
    return client_ids


def test_default_capacity_is_still_one_cell_per_profile(session):
    """The pre-task-31 contract: without an explicit capacity, a profile runs
    exactly one cell at a time."""
    _seed(session, 3)
    cells = CellRepository(session)

    first = cells.claim_next_cell(Provider.ahrefs, lease_owner="w")
    second = cells.claim_next_cell(Provider.ahrefs, lease_owner="w")

    assert first is not None
    assert second is None


def test_capacity_allows_exactly_n_concurrent_cells(session):
    _seed(session, 5)
    cells = CellRepository(session)

    claimed = [cells.claim_next_cell(Provider.ahrefs, lease_owner="w", profile_capacity=3)
               for _ in range(4)]

    assert [c is not None for c in claimed] == [True, True, True, False]
    running = [c for c in claimed if c is not None]
    assert {c.status for c in running} == {CellStatus.running}
    assert len({c.id for c in running}) == 3, "the same cell was claimed twice"


def test_finishing_a_cell_frees_its_place(session):
    _seed(session, 3)
    cells = CellRepository(session)
    first = cells.claim_next_cell(Provider.ahrefs, lease_owner="w", profile_capacity=2)
    cells.claim_next_cell(Provider.ahrefs, lease_owner="w", profile_capacity=2)

    assert cells.claim_next_cell(Provider.ahrefs, lease_owner="w", profile_capacity=2) is None

    cells.set_status(first.id, CellStatus.succeeded)

    assert cells.claim_next_cell(Provider.ahrefs, lease_owner="w", profile_capacity=2) is not None


def test_claim_records_the_slot_it_was_given(session):
    _seed(session, 2)
    cells = CellRepository(session)

    cell = cells.claim_next_cell(Provider.ahrefs, lease_owner="w", profile_capacity=2, slot=1)

    assert cell.slot == 1


def test_retry_clears_the_slot_of_the_previous_attempt(session):
    _seed(session, 1)
    cells = CellRepository(session)
    cell = cells.claim_next_cell(Provider.ahrefs, lease_owner="w", profile_capacity=2, slot=1)
    cells.set_status(cell.id, CellStatus.failed, error_code="boom")

    requeued = cells.requeue_for_retry(cell.id)

    assert requeued.slot is None


def test_cells_without_a_profile_are_not_capacity_limited(session):
    """API-only integrations have no browser profile — nothing to serialise, so
    the semaphore must not invent a limit for them."""
    _seed(session, 3, profile=None)
    cells = CellRepository(session)

    claimed = [cells.claim_next_cell(Provider.ahrefs, lease_owner="w") for _ in range(3)]

    assert all(cell is not None for cell in claimed)


def test_profile_slots_free_counts_down_and_never_goes_negative(session):
    _seed(session, 3)
    cells = CellRepository(session)
    key = "browser:main"

    assert cells.profile_slots_free(key, capacity=2) == 2
    cells.claim_next_cell(Provider.ahrefs, lease_owner="w", profile_capacity=2)
    assert cells.profile_slots_free(key, capacity=2) == 1
    cells.claim_next_cell(Provider.ahrefs, lease_owner="w", profile_capacity=2)
    assert cells.profile_slots_free(key, capacity=2) == 0
    assert cells.profile_slots_free(key, capacity=1) == 0


def test_acquire_profile_lock_still_answers_the_single_slot_question(session):
    _seed(session, 2)
    cells = CellRepository(session)

    assert cells.acquire_profile_lock("browser:main") is True
    cells.claim_next_cell(Provider.ahrefs, lease_owner="w")
    assert cells.acquire_profile_lock("browser:main") is False


def test_concurrent_lanes_never_exceed_capacity(temp_db):
    """The check lives inside the claim UPDATE precisely so this cannot happen:
    two lanes reading "one free slot" in the same instant would both proceed if
    it were a separate SELECT."""
    with temp_db.session_scope() as session:
        _seed(session, 12)

    capacity = 3
    claimed: list[str] = []
    lock = threading.Lock()
    start = threading.Barrier(8)

    def lane() -> None:
        start.wait()
        with temp_db.session_scope() as session:
            cell = CellRepository(session).claim_next_cell(
                Provider.ahrefs, lease_owner="w", profile_capacity=capacity
            )
            if cell is not None:
                with lock:
                    claimed.append(cell.id)

    threads = [threading.Thread(target=lane) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(claimed) <= capacity, f"capacity {capacity} exceeded: {len(claimed)} cells running"
    assert len(set(claimed)) == len(claimed), "the same cell was claimed by two lanes"
    with temp_db.session_scope() as session:
        running = [
            cell
            for cell in CellRepository(session).list_for_batch(_batch_id(session))
            if cell.status == CellStatus.running
        ]
    assert len(running) == len(claimed)


def _batch_id(session) -> str:
    from app.db.models import SyncJobProvider

    return session.query(SyncJobProvider).first().batch_id
