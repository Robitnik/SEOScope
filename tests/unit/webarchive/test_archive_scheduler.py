from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from app.webarchive.scheduler import _parse_slot_times, most_recent_slot, slot_key_for

KYIV = ZoneInfo("Europe/Kyiv")
SLOTS = _parse_slot_times(["13:30", "19:30"])


def test_most_recent_slot_same_day_after_second_slot() -> None:
    now = datetime(2026, 9, 4, 20, 0, tzinfo=KYIV)
    result = most_recent_slot(now, SLOTS)
    assert (result.hour, result.minute) == (19, 30)
    assert result.date() == now.date()


def test_most_recent_slot_between_slots() -> None:
    now = datetime(2026, 9, 4, 15, 0, tzinfo=KYIV)
    result = most_recent_slot(now, SLOTS)
    assert (result.hour, result.minute) == (13, 30)
    assert result.date() == now.date()


def test_most_recent_slot_before_first_slot_falls_back_to_yesterday() -> None:
    now = datetime(2026, 9, 4, 6, 0, tzinfo=KYIV)
    result = most_recent_slot(now, SLOTS)
    assert result.date() == now.date() - timedelta(days=1)
    assert (result.hour, result.minute) == (19, 30)


def test_most_recent_slot_exact_boundary_is_inclusive() -> None:
    now = datetime(2026, 9, 4, 13, 30, tzinfo=KYIV)
    result = most_recent_slot(now, SLOTS)
    assert (result.hour, result.minute) == (13, 30)
    assert result.date() == now.date()


def test_slot_key_includes_utc_offset() -> None:
    moment = datetime(2026, 9, 4, 13, 30, tzinfo=KYIV)
    key = slot_key_for(moment)
    assert key.startswith("2026-09-04T13:30")
    assert key.endswith("+03:00")


def test_slot_key_distinguishes_dst_transition() -> None:
    # Kyiv leaves DST on the last Sunday of October — the same wall-clock
    # HH:MM either side of it must never collide onto one `slot_key`
    # (task-59 §1.4/§11: DST double-run or skipped-run risk).
    before_dst_end = datetime(2026, 10, 24, 13, 30, tzinfo=KYIV)
    after_dst_end = datetime(2026, 11, 1, 13, 30, tzinfo=KYIV)
    assert slot_key_for(before_dst_end) != slot_key_for(after_dst_end)
    assert "+03:00" in slot_key_for(before_dst_end)
    assert "+02:00" in slot_key_for(after_dst_end)


def test_reclaim_dead_captures_frees_a_row_this_process_is_not_running(session, tmp_path) -> None:
    """Live incident (task-59, 2026-09-04): a `database is locked` burst
    crashed six captures at once AND blocked the `finish_capture` in their
    own crash handler, leaving rows `running` for three hours. Because
    `_tick` skips any client with `has_running_capture`, each of those rows
    also locked its client out of every later slot.
    """
    from contextlib import contextmanager

    from app.core.config import Settings
    from app.db.repositories.clients import ClientRepository
    from app.db.repositories.web_archive import WebArchiveRepository
    from app.webarchive.scheduler import ArchiveScheduler

    client = ClientRepository(session).create(
        name="Acme", slug="acme", domain="acme.example.com", timezone="America/New_York"
    )
    session.commit()
    repo = WebArchiveRepository(session)
    zombie = repo.create_capture(
        client.id, slot_key="2026-09-04T13:30+03:00",
        scheduled_for=datetime(2026, 9, 4, 10, 30, tzinfo=UTC), trigger="scheduled",
    )
    repo.claim_capture(zombie.id)
    session.commit()
    assert repo.has_running_capture(client.id) is True

    @contextmanager
    def _scope():
        yield session

    scheduler = ArchiveScheduler(
        session_scope=_scope,
        settings=Settings(_env_file=None, artifacts_dir=tmp_path),
    )
    scheduler._reclaim_dead_captures()

    reclaimed = repo.get_capture(zombie.id)
    assert reclaimed.status == "failed"
    assert reclaimed.error_code == "interrupted"
    assert reclaimed.finished_at is not None
    # The client is free to be captured again on the very next tick.
    assert repo.has_running_capture(client.id) is False


def test_reclaim_dead_captures_leaves_this_process_own_run_alone(session, tmp_path) -> None:
    from contextlib import contextmanager

    from app.core.config import Settings
    from app.db.repositories.clients import ClientRepository
    from app.db.repositories.web_archive import WebArchiveRepository
    from app.webarchive.scheduler import ArchiveScheduler

    client = ClientRepository(session).create(
        name="Acme", slug="acme", domain="acme.example.com", timezone="America/New_York"
    )
    session.commit()
    repo = WebArchiveRepository(session)
    capture = repo.create_capture(
        client.id, slot_key="2026-09-04T13:30+03:00",
        scheduled_for=datetime(2026, 9, 4, 10, 30, tzinfo=UTC), trigger="scheduled",
    )
    repo.claim_capture(capture.id)
    session.commit()

    @contextmanager
    def _scope():
        yield session

    scheduler = ArchiveScheduler(
        session_scope=_scope,
        settings=Settings(_env_file=None, artifacts_dir=tmp_path),
    )
    scheduler._in_flight.add(capture.id)  # a live capture, however long it runs
    scheduler._reclaim_dead_captures()

    assert repo.get_capture(capture.id).status == "running"
