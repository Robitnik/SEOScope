"""Repository-level tests (task-60) for the new query methods that back
the backfill CLI (§1.3) and the hub's Asset coverage tile (§1.4).
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.db.repositories.clients import ClientRepository
from app.db.repositories.web_archive import WebArchiveRepository
from app.webarchive.replay import format_ts, parse_ts


def _client(session, *, slug: str = "acme"):
    client = ClientRepository(session).create(
        name="Acme", slug=slug, domain=f"{slug}.com", timezone="America/New_York"
    )
    session.commit()
    return client


def _snapshot(session, repo, client, *, url: str = "https://acme.com/", observed_at=None):
    archive_url = repo.get_or_create_url(
        client.id, url, url.replace("https://", ""), source="manual"
    )
    at = observed_at or datetime(2026, 1, 1, tzinfo=UTC)
    snapshot = repo.create_snapshot(
        archive_url_id=archive_url.id,
        client_id=client.id,
        capture_id=None,
        observed_at=at,
        http_status=200,
        final_url=url,
        headers_json={},
        content_type="text/html",
        content_fingerprint="deadbeef",
        raw_sha256="a" * 64,
        body_blob_sha256=None,
        title=None, description=None, canonical=None, meta_robots=None, word_count=1,
        asset_count=0, assets_truncated=False, store_reason="first", prev_snapshot_id=None,
        last_confirmed_at=at, confirmed_count=1,
    )
    session.commit()
    return archive_url, snapshot


def test_all_snapshots_scopes_by_client(session):
    repo = WebArchiveRepository(session)
    client_a = _client(session, slug="acme")
    client_b = _client(session, slug="beta")
    _snapshot(session, repo, client_a, url="https://acme.com/")
    _snapshot(session, repo, client_b, url="https://beta.com/")

    assert len(repo.all_snapshots()) == 2
    assert len(repo.all_snapshots(client_a.id)) == 1
    assert repo.all_snapshots(client_a.id)[0].client_id == client_a.id


def test_css_assets_for_client_only_returns_css_that_clients_own_snapshots_link(session):
    repo = WebArchiveRepository(session)
    client_a = _client(session, slug="acme")
    client_b = _client(session, slug="beta")
    _archive_url_a, snapshot_a = _snapshot(session, repo, client_a, url="https://acme.com/")
    _archive_url_b, snapshot_b = _snapshot(session, repo, client_b, url="https://beta.com/")

    css_a = repo.get_or_create_asset("https://cdn.com/a.css", "cdn.com/a.css", kind="css")
    css_b = repo.get_or_create_asset("https://cdn.com/b.css", "cdn.com/b.css", kind="css")
    img = repo.get_or_create_asset("https://cdn.com/logo.png", "cdn.com/logo.png", kind="img")
    repo.link_snapshot_asset(
        snapshot_id=snapshot_a.id, asset_id=css_a.id, asset_version_id=None, role="css"
    )
    repo.link_snapshot_asset(
        snapshot_id=snapshot_a.id, asset_id=img.id, asset_version_id=None, role="img"
    )
    repo.link_snapshot_asset(
        snapshot_id=snapshot_b.id, asset_id=css_b.id, asset_version_id=None, role="css"
    )
    session.commit()

    result = repo.css_assets_for_client(client_a.id)
    assert {a.id for a in result} == {css_a.id}  # neither client B's css nor the img asset
    assert {a.id for a in repo.all_css_assets()} == {css_a.id, css_b.id}


def test_asset_coverage_totals_counts_referenced_vs_captured(session):
    repo = WebArchiveRepository(session)
    repo.register_blob(
        "a" * 64, size_bytes=3, gzip_size_bytes=3, content_type="image/jpeg", path="x"
    )
    captured = repo.get_or_create_asset("https://cdn.com/a.jpg", "cdn.com/a.jpg", kind="img")
    repo.create_asset_version(
        asset_id=captured.id, blob_sha256="a" * 64, content_type="image/jpeg",
        http_status=200, first_seen_at=datetime(2026, 1, 1, tzinfo=UTC),
        last_confirmed_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    # never fetched — no version, so it counts as referenced but not captured
    repo.get_or_create_asset("https://cdn.com/dead.jpg", "cdn.com/dead.jpg", kind="img")
    session.commit()

    totals = repo.asset_coverage_totals()
    assert totals == {"referenced": 2, "captured": 1}


def test_queued_captures_are_fifo_by_queued_at(session):
    """task-62 §1.3: one queue, no priority class — a manual and a scheduled
    trigger both just join the tail in the order they were created.
    """
    repo = WebArchiveRepository(session)
    client = _client(session)
    first = repo.create_capture(
        client.id, slot_key="a", scheduled_for=datetime(2026, 1, 1, tzinfo=UTC),
        trigger="scheduled", now=datetime(2026, 1, 1, 10, 0, tzinfo=UTC),
    )
    second = repo.create_capture(
        client.id, slot_key="b", scheduled_for=datetime(2026, 1, 1, tzinfo=UTC),
        trigger="manual", now=datetime(2026, 1, 1, 9, 0, tzinfo=UTC),
    )
    session.commit()

    ordered = repo.queued_captures()
    assert [c.id for c in ordered] == [second.id, first.id]  # earlier queued_at first


def test_has_active_capture_true_for_both_queued_and_running(session):
    """task-62 §1.3: guarding only `running` (the pre-task-62 behaviour) let
    two rapid "Capture now" clicks queue two rows for the same client.
    """
    repo = WebArchiveRepository(session)
    client = _client(session)
    assert repo.has_active_capture(client.id) is False

    queued = repo.create_capture(
        client.id, slot_key="a", scheduled_for=datetime(2026, 1, 1, tzinfo=UTC), trigger="manual"
    )
    session.commit()
    assert repo.has_active_capture(client.id) is True
    assert repo.has_running_capture(client.id) is False  # still just queued

    repo.claim_capture(queued.id, owner="test")
    session.commit()
    assert repo.has_active_capture(client.id) is True
    assert repo.has_running_capture(client.id) is True


def test_claim_capture_refuses_a_second_queued_row_for_the_same_client(session):
    """The admission check now lives INSIDE `claim_capture` itself (task-62
    §2.3) rather than only at `POST /captures` time — two queued rows for
    one client (however they got created) can never both end up `running`.
    """
    repo = WebArchiveRepository(session)
    client = _client(session)
    first = repo.create_capture(
        client.id, slot_key="a", scheduled_for=datetime(2026, 1, 1, tzinfo=UTC), trigger="manual"
    )
    second = repo.create_capture(
        client.id, slot_key="b", scheduled_for=datetime(2026, 1, 1, tzinfo=UTC), trigger="manual"
    )
    session.commit()

    claimed_first = repo.claim_capture(first.id, owner="worker")
    session.commit()
    assert claimed_first is not None

    claimed_second = repo.claim_capture(second.id, owner="worker")
    assert claimed_second is None  # refused — client A already has one running
    assert repo.get_capture(second.id).status == "queued"  # untouched, still claimable later


def test_request_cancel_on_queued_row_finishes_it_immediately(session):
    repo = WebArchiveRepository(session)
    client = _client(session)
    capture = repo.create_capture(
        client.id, slot_key="a", scheduled_for=datetime(2026, 1, 1, tzinfo=UTC), trigger="manual"
    )
    session.commit()

    result = repo.request_cancel(capture.id)

    assert result == "cancelled"
    stored = repo.get_capture(capture.id)
    assert stored.status == "cancelled"
    assert stored.finished_at is not None


def test_request_cancel_on_running_row_only_flags_it(session):
    repo = WebArchiveRepository(session)
    client = _client(session)
    capture = repo.create_capture(
        client.id, slot_key="a", scheduled_for=datetime(2026, 1, 1, tzinfo=UTC), trigger="manual"
    )
    session.commit()
    repo.claim_capture(capture.id, owner="worker")
    session.commit()

    result = repo.request_cancel(capture.id)

    assert result == "cancel_requested"
    stored = repo.get_capture(capture.id)
    assert stored.status == "running"  # the capture loop stops itself, not this call
    assert stored.cancel_requested is True


def test_request_cancel_on_terminal_row_is_a_noop(session):
    repo = WebArchiveRepository(session)
    client = _client(session)
    capture = repo.create_capture(
        client.id, slot_key="a", scheduled_for=datetime(2026, 1, 1, tzinfo=UTC), trigger="manual"
    )
    session.commit()
    repo.finish_capture(capture.id, status="ok")
    session.commit()

    assert repo.request_cancel(capture.id) is None
    assert repo.get_capture(capture.id).status == "ok"


def test_append_event_seq_is_monotonic_per_capture_and_events_since_returns_the_tail(session):
    repo = WebArchiveRepository(session)
    client = _client(session)
    capture_a = repo.create_capture(
        client.id, slot_key="a", scheduled_for=datetime(2026, 1, 1, tzinfo=UTC), trigger="manual"
    )
    capture_b = repo.create_capture(
        client.id, slot_key="b", scheduled_for=datetime(2026, 1, 1, tzinfo=UTC), trigger="manual"
    )
    session.commit()

    seq1 = repo.append_event(capture_a.id, "debug", stage="url", message="first")
    seq2 = repo.append_event(capture_a.id, "warning", stage="url", message="second")
    # A different capture's own numbering starts fresh at 1 — `seq` is
    # per-capture, not a shared global counter (unlike `job_events.seq`).
    seq_b = repo.append_event(capture_b.id, "info", message="unrelated")
    session.commit()

    assert (seq1, seq2, seq_b) == (1, 2, 1)
    tail = repo.events_since(capture_a.id, seq1)
    assert [e.message for e in tail] == ["second"]
    assert repo.events_since(capture_a.id, 0)[0].level == "debug"


def test_append_event_redacts_nothing_by_itself_but_stores_details_json(session):
    """PLAN §13 (no secrets in events) is enforced by the CALLER choosing
    what to pass as `details` — this just verifies the plumbing round-trips
    structured data faithfully, which is what a redaction step would rely on.
    """
    repo = WebArchiveRepository(session)
    client = _client(session)
    capture = repo.create_capture(
        client.id, slot_key="a", scheduled_for=datetime(2026, 1, 1, tzinfo=UTC), trigger="manual"
    )
    session.commit()

    repo.append_event(
        capture.id, "error", stage="url", message="failed", details={"status": 500}
    )
    session.commit()

    event = repo.events_since(capture.id, 0)[0]
    assert event.details_json == {"status": 500}


# --- resolve_snapshot / resolve_asset_version off-by-one (task-63 §2.1) ---
#
# `ts` (14-digit, second precision) always sits a whole second BEFORE the
# snapshot's own `observed_at` (which carries microseconds) — a snapshot
# captured at 13:40:46.280052 has its own `ts` = "20260904134046",
# i.e. 13:40:46.000000. A literal `observed_at <= at` therefore excludes
# the snapshot from resolving to ITSELF. Live-confirmed on a real
# multi-version URL: every one of 4 versions resolved one version stale.

def test_resolve_snapshot_resolves_a_version_to_itself(session):
    repo = WebArchiveRepository(session)
    client = _client(session)
    _, snapshot = _snapshot(
        session, repo, client, observed_at=datetime(2026, 9, 4, 13, 40, 46, 280052, tzinfo=UTC)
    )

    own_ts = parse_ts(format_ts(snapshot.observed_at))
    resolved, is_exact = repo.resolve_snapshot(snapshot.archive_url_id, own_ts)

    assert resolved is not None
    assert resolved.id == snapshot.id
    assert is_exact is True


def test_resolve_snapshot_picks_the_right_one_of_several_versions(session):
    repo = WebArchiveRepository(session)
    client = _client(session)
    archive_url = repo.get_or_create_url(client.id, "https://acme.com/", "acme.com/", source="manual")
    session.commit()

    def _make(observed_at):
        return repo.create_snapshot(
            archive_url_id=archive_url.id, client_id=client.id, capture_id=None,
            observed_at=observed_at, http_status=200, final_url="https://acme.com/",
            headers_json={}, content_type="text/html", content_fingerprint="deadbeef",
            raw_sha256="a" * 64, body_blob_sha256=None,
            title=None, description=None, canonical=None, meta_robots=None, word_count=1,
            asset_count=0, assets_truncated=False, store_reason="first", prev_snapshot_id=None,
            last_confirmed_at=observed_at, confirmed_count=1,
        )

    oldest = _make(datetime(2026, 9, 4, 10, 31, 4, 164922, tzinfo=UTC))
    middle = _make(datetime(2026, 9, 4, 13, 40, 46, 260870, tzinfo=UTC))
    newest = _make(datetime(2026, 9, 4, 16, 30, 30, 258380, tzinfo=UTC))
    session.commit()

    for target in (oldest, middle, newest):
        own_ts = parse_ts(format_ts(target.observed_at))
        resolved, _is_exact = repo.resolve_snapshot(archive_url.id, own_ts)
        assert resolved.id == target.id, f"ts for {target.id} resolved to {resolved.id}"

    # A moment strictly BEFORE the first capture still falls back to the
    # earliest version (`is_exact=False`) — the off-by-one fix must not
    # regress this existing "nearest capture" convention.
    before_anything = parse_ts("20260101000000")
    resolved, is_exact = repo.resolve_snapshot(archive_url.id, before_anything)
    assert resolved.id == oldest.id
    assert is_exact is False


def test_resolve_asset_version_resolves_a_version_to_itself(session):
    repo = WebArchiveRepository(session)
    repo.register_blob("a" * 64, size_bytes=3, gzip_size_bytes=3, content_type="text/css", path="x")
    asset = repo.get_or_create_asset("https://cdn.com/a.css", "cdn.com/a.css", kind="css")
    first_seen = datetime(2026, 9, 4, 13, 40, 46, 280052, tzinfo=UTC)
    version = repo.create_asset_version(
        asset_id=asset.id, blob_sha256="a" * 64, content_type="text/css",
        http_status=200, first_seen_at=first_seen, last_confirmed_at=first_seen,
    )
    session.commit()

    own_ts = parse_ts(format_ts(first_seen))
    resolved, is_exact = repo.resolve_asset_version(asset.id, own_ts)

    assert resolved is not None
    assert resolved.id == version.id
    assert is_exact is True


# --- find_asset_by_key_prefix (task-63 §3.2) ------------------------------

def test_find_asset_by_key_prefix_matches_a_different_query_string(session):
    repo = WebArchiveRepository(session)
    repo.get_or_create_asset(
        "https://acme.com/widget.css?v=1788000000000",
        "acme.com/widget.css?v=1788000000000",
        kind="css",
    )
    session.commit()

    found = repo.find_asset_by_key_prefix("acme.com/widget.css?")
    assert found is not None
    assert found.url_key == "acme.com/widget.css?v=1788000000000"


def test_find_asset_by_key_prefix_does_not_match_a_different_file(session):
    repo = WebArchiveRepository(session)
    repo.get_or_create_asset("https://acme.com/other.css", "acme.com/other.css", kind="css")
    session.commit()

    assert repo.find_asset_by_key_prefix("acme.com/widget.css?") is None


def test_find_asset_by_key_prefix_escapes_like_wildcards_in_the_prefix(session):
    # A real path segment can legitimately contain `%`/`_` — these must be
    # matched LITERALLY, not treated as SQL LIKE wildcards (which would
    # match any-and-everything and defeat the whole point of a prefix scan).
    repo = WebArchiveRepository(session)
    repo.get_or_create_asset(
        "https://acme.com/a_b%20c.css?v=1", "acme.com/a_b%20c.css?v=1", kind="css"
    )
    repo.get_or_create_asset("https://acme.com/aXbYYc.css?v=1", "acme.com/aXbYYc.css?v=1", kind="css")
    session.commit()

    found = repo.find_asset_by_key_prefix("acme.com/a_b%20c.css?")
    assert found is not None
    assert found.url_key == "acme.com/a_b%20c.css?v=1"


def test_count_replay_misses_for_host(session):
    repo = WebArchiveRepository(session)
    repo.record_replay_miss(
        "https://acme.com/missing.js", "acme.com/missing.js"
    )
    repo.record_replay_miss(
        "https://other.com/missing.js", "other.com/missing.js"
    )
    session.commit()

    assert repo.count_replay_misses_for_host("acme.com") == 1
    assert repo.count_replay_misses_for_host("other.com") == 1
    assert repo.count_replay_misses_for_host("nowhere.com") == 0
