"""task-65: `WebArchiveRepository` must never flush as a side effect of a
single method call — the writer lock SQLite takes at the first write of a
transaction is what task-65 stopped acquiring early. A genuinely concurrent
version of this test (real threads on a file-backed engine) is a load test,
not a unit test (see `test_capture.py`'s own regression test and its
docstring) — this file instead pins the invariant directly by counting
actual `flush()` calls with a SQLAlchemy event, which is deterministic and
takes milliseconds.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime

from sqlalchemy import event, text

from app.db.repositories.clients import ClientRepository
from app.db.repositories.web_archive import WebArchiveRepository
from app.monitor.fetcher import FetchResult
from app.webarchive import capture as capturemod


@contextmanager
def _flush_counter(session):
    count = 0

    def _listener(*_args, **_kwargs):
        nonlocal count
        count += 1

    event.listen(session, "after_flush", _listener)
    try:
        yield lambda: count
    finally:
        event.remove(session, "after_flush", _listener)


def _client(session):
    client = ClientRepository(session).create(
        name="Acme", slug="acme", domain="acme.example.com", timezone="America/New_York"
    )
    session.commit()
    return client


def test_get_or_create_asset_does_not_flush(session):
    """The one confirmed live bug (task-65 §0.1): this used to flush BEFORE
    `_check_or_fetch_asset`'s own network fetch for a brand-new asset,
    holding the writer lock across it.
    """
    repo = WebArchiveRepository(session)
    with _flush_counter(session) as flushes:
        asset = repo.get_or_create_asset(
            "https://cdn.example.com/a.png", "cdn.example.com/a.png", kind="img"
        )
    assert flushes() == 0
    assert asset.id is not None  # available without a flush


def test_mutating_an_existing_row_does_not_flush(session):
    """`claim_capture`/`update_capture_counters`/etc. all just `setattr` an
    already-loaded row — nothing here needs a flush to be readable by the
    caller, and the eventual `write_controller.submit`/`commit_with_retry`
    commit is what actually persists it.
    """
    repo = WebArchiveRepository(session)
    client = _client(session)
    capture = repo.create_capture(
        client.id, slot_key="manual:1", scheduled_for=datetime(2026, 1, 1, tzinfo=UTC),
        trigger="manual",
    )
    session.commit()

    with _flush_counter(session) as flushes:
        claimed = repo.claim_capture(capture.id, owner="test")
        repo.update_capture_counters(capture.id, urls_total=5, heartbeat_at=datetime.now(UTC))
        repo.finish_capture(capture.id, status="ok")
    assert flushes() == 0
    assert claimed is not None
    assert claimed.status == "ok"  # in-memory attribute, correct without any flush


def test_create_methods_populate_id_without_a_flush(session):
    """`id=new_uuid()` at construction (task-65 §1.1) — the whole reason a
    caller no longer needs `create_x`/`get_or_create_x` to flush to get
    `.id` back.

    Each method is checked in its OWN `_flush_counter` block, not chained in
    one — chaining three creates back to back with no commit in between
    means the SECOND method's own existence-check query legitimately
    autoflushes the FIRST one's still-pending row (default SQLAlchemy
    `autoflush`, unrelated to this file calling `.flush()` itself); real
    callers don't do that (each of these normally commits, via
    `write_controller.submit`, before the next one runs), so asserting
    across a contrived chain would be testing an artifact, not the
    invariant this file actually guarantees: no method here EVER calls
    `session.flush()` on its own.
    """
    repo = WebArchiveRepository(session)
    client = _client(session)

    with _flush_counter(session) as flushes:
        capture = repo.create_capture(
            client.id, slot_key="manual:2", scheduled_for=datetime(2026, 1, 1, tzinfo=UTC),
            trigger="manual",
        )
    assert flushes() == 0
    session.commit()

    with _flush_counter(session) as flushes:
        archive_url = repo.get_or_create_url(
            client.id, "https://acme.example.com/", "acme.example.com/", source="manual"
        )
    assert flushes() == 0
    session.commit()

    with _flush_counter(session) as flushes:
        settings = repo.get_or_create_settings(client.id)
    assert flushes() == 0
    assert capture.id and archive_url.id and settings.id


def test_asset_fetch_happens_before_its_own_row_is_flushed(
    session, artifacts_dir, monkeypatch
):
    """The actual regression, end to end: for a brand-new asset, the
    network fetch must run while `asset` is still just a staged, unflushed
    object — proving the writer lock (taken only at an actual flush/commit)
    was never held across it.
    """
    repo = WebArchiveRepository(session)
    seen_asset_flushed: list[bool] = []

    def _fake_fetch(url, **kwargs):
        # At the moment of the network call, the asset row this capture
        # is about to check must NOT be visible to a fresh query yet —
        # that would mean it was flushed (and so the writer lock taken)
        # before this fetch, not after.
        with session.no_autoflush:
            row = session.execute(
                text("SELECT 1 FROM archive_assets WHERE url_key = :k"),
                {"k": "cdn.example.com/new-asset.png"},
            ).first()
        seen_asset_flushed.append(row is not None)
        return FetchResult(
            requested_url=url, final_url=url, status=200, hop_count=0,
            headers={"Content-Type": "image/png"}, body=b"PNGDATA",
            truncated_body=False, elapsed_ms=1, error=None,
        )

    monkeypatch.setattr(capturemod, "fetch", _fake_fetch)

    from app.core.config import Settings
    from app.tools.http_safety import HostThrottle
    from app.webarchive.capture import FetchStats, _check_or_fetch_asset

    asset = repo.get_or_create_asset(
        "https://cdn.example.com/new-asset.png", "cdn.example.com/new-asset.png", kind="img"
    )
    settings = Settings(_env_file=None, artifacts_dir=artifacts_dir)
    _check_or_fetch_asset(
        repo, asset, settings=settings, artifacts_dir=artifacts_dir,
        throttle=HostThrottle(min_interval_seconds=0.0), stats=FetchStats(),
        now=datetime(2026, 1, 1, tzinfo=UTC),
    )

    assert seen_asset_flushed == [False]  # not flushed yet at fetch time
