from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from app.monitor.fetcher import FetchResult
from app.webarchive import capture as capturemod
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


def test_reclaim_dead_captures_frees_a_row_with_a_stale_heartbeat(session, tmp_path) -> None:
    """Live incident (task-59, 2026-09-04): a `database is locked` burst
    crashed six captures at once AND blocked the `finish_capture` in their
    own crash handler, leaving rows `running` for three hours. Because
    `_tick` skips any client with `has_running_capture`, each of those rows
    also locked its client out of every later slot.

    task-62 §2.3: liveness is `heartbeat_at`, not membership in an
    in-process set — a row whose heartbeat was last touched long ago (here,
    at claim time, back-dated to simulate a process that died right after)
    is reclaimed regardless of which process claimed it.
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
    repo.claim_capture(zombie.id, owner="test", now=datetime(2026, 9, 4, 10, 30, tzinfo=UTC))
    session.commit()
    assert repo.has_running_capture(client.id) is True

    @contextmanager
    def _scope():
        yield session

    scheduler = ArchiveScheduler(
        session_scope=_scope,
        settings=Settings(_env_file=None, artifacts_dir=tmp_path),
    )
    scheduler._write_controller.start()
    try:
        scheduler._reclaim_dead_captures()
    finally:
        scheduler._write_controller.stop()

    reclaimed = repo.get_capture(zombie.id)
    assert reclaimed.status == "failed"
    assert reclaimed.error_code == "interrupted"
    assert reclaimed.finished_at is not None
    # The client is free to be captured again on the very next tick.
    assert repo.has_running_capture(client.id) is False


def test_reclaim_dead_captures_leaves_a_fresh_heartbeat_alone(session, tmp_path) -> None:
    """A capture with a heartbeat from just now is alive — whether this
    scheduler's OWN process claimed it (the worker's usual case) or a
    DIFFERENT process did (`yovth-report archive-capture` in its own OS
    process, keeping its own heartbeat current on the same row): the
    heartbeat carries no notion of which process, on purpose, since that
    cross-process case is exactly the false-kill this replaces (task-62
    §0.4, reproduced live 2026-09-05: a CLI capture was declared
    `interrupted` within a minute of starting while it kept working).
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
    capture = repo.create_capture(
        client.id, slot_key="2026-09-04T13:30+03:00",
        scheduled_for=datetime(2026, 9, 4, 10, 30, tzinfo=UTC), trigger="scheduled",
    )
    # No `now=` override — a genuinely fresh, real-wall-clock heartbeat,
    # exactly as a capture running RIGHT NOW (in this process or another)
    # would have, regardless of who claimed it.
    repo.claim_capture(capture.id, owner="cli")
    session.commit()

    @contextmanager
    def _scope():
        yield session

    scheduler = ArchiveScheduler(
        session_scope=_scope,
        settings=Settings(_env_file=None, artifacts_dir=tmp_path),
    )
    scheduler._reclaim_dead_captures()

    assert repo.get_capture(capture.id).status == "running"


def test_submit_reads_scope_url_and_captures_exactly_one_url(
    session, tmp_path, monkeypatch
) -> None:
    """Regression (caught live, 2026-09-05): `_submit`/`_run` claimed a
    `scope="url"` row but never read `archive_url_id` back off it, so
    `run_client_capture` silently fell back to the whole client's URL list
    — a "Refresh now" on one GOREPORT page captured 2 URLs instead of 1.
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
    url_a = repo.get_or_create_url(
        client.id, "https://acme.example.com/a", "acme.example.com/a", source="manual"
    )
    repo.get_or_create_url(
        client.id, "https://acme.example.com/b", "acme.example.com/b", source="manual"
    )
    session.commit()

    def _fake_fetch(url, **kwargs):
        return FetchResult(
            requested_url=url, final_url=url, status=200, hop_count=0,
            headers={"Content-Type": "text/html"},
            body=b"<html><head><title>T</title></head><body>hi</body></html>",
            truncated_body=False, elapsed_ms=1, error=None,
        )

    monkeypatch.setattr(capturemod, "fetch", _fake_fetch)

    capture = repo.create_capture(
        client.id, slot_key="manual:scope-test", scheduled_for=datetime.now(UTC),
        trigger="manual", scope="url", archive_url_id=url_a.id,
    )
    session.commit()

    @contextmanager
    def _scope():
        yield session

    settings = Settings(
        _env_file=None, artifacts_dir=tmp_path, archive_host_min_interval_seconds=0.0,
        archive_client_workers=1,
    )
    scheduler = ArchiveScheduler(session_scope=_scope, settings=settings)
    scheduler._write_controller.start()
    try:
        scheduler._submit(client, capture.id)
        scheduler._pool.shutdown(wait=True)
    finally:
        scheduler._write_controller.stop()

    finished = repo.get_capture(capture.id)
    assert finished.status == "ok"
    assert finished.urls_total == 1  # not 2 — the OTHER url must stay untouched


def test_submit_reads_force_and_writes_forced_snapshot_on_unchanged_page(
    session, tmp_path, monkeypatch
) -> None:
    """task-80 — the exact class of defect the `scope`/`archive_url_id` test
    right above documents: without reading `force` back off the claimed row,
    `_submit` would silently run an ordinary capture and "Recapture" would
    do nothing observable on an unchanged page.
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
    archive_url = repo.get_or_create_url(
        client.id, "https://acme.example.com/", "acme.example.com/", source="manual"
    )
    session.commit()
    html = b"<html><head><title>T</title></head><body>hi</body></html>"

    def _fake_fetch(url, **kwargs):
        return FetchResult(
            requested_url=url, final_url=url, status=200, hop_count=0,
            headers={"Content-Type": "text/html"}, body=html,
            truncated_body=False, elapsed_ms=1, error=None,
        )

    monkeypatch.setattr(capturemod, "fetch", _fake_fetch)

    @contextmanager
    def _scope():
        yield session

    settings = Settings(
        _env_file=None, artifacts_dir=tmp_path, archive_host_min_interval_seconds=0.0,
        archive_client_workers=1,
    )

    # First pass, NOT forced — establishes an existing snapshot so the second
    # pass has something to (not) confirm instead of writing a "first".
    first_capture = repo.create_capture(
        client.id, slot_key="manual:force-test-1", scheduled_for=datetime.now(UTC),
        trigger="manual",
    )
    session.commit()
    scheduler = ArchiveScheduler(session_scope=_scope, settings=settings)
    scheduler._write_controller.start()
    try:
        scheduler._submit(client, first_capture.id)
        scheduler._pool.shutdown(wait=True)
    finally:
        scheduler._write_controller.stop()
    assert repo.latest_snapshot(archive_url.id).store_reason == "first"

    # Second pass, forced, same bytes — an ordinary pass would just confirm
    # the existing snapshot and write nothing new.
    forced_capture = repo.create_capture(
        client.id, slot_key="manual:force-test-2", scheduled_for=datetime.now(UTC),
        trigger="manual", force=True,
    )
    session.commit()
    scheduler = ArchiveScheduler(session_scope=_scope, settings=settings)
    scheduler._write_controller.start()
    try:
        scheduler._submit(client, forced_capture.id)
        scheduler._pool.shutdown(wait=True)
    finally:
        scheduler._write_controller.stop()

    finished = repo.get_capture(forced_capture.id)
    assert finished.status == "ok"
    snapshots = repo.list_snapshots_for_url(archive_url.id)  # newest first
    assert len(snapshots) == 2
    assert snapshots[0].store_reason == "forced"
