"""Інспекція і синк sitemap (task-100 §2.3, кроки 1-2)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from app.db.models import IndexingUrl
from app.db.repositories.indexing import IndexingRepository
from app.indexing.inspect import is_indexed_state, select_for_inspection
from app.indexing.sync import SyncResult, _parse_lastmod

NOW = datetime(2026, 9, 21, tzinfo=UTC)


@pytest.mark.parametrize(
    ("state", "verdict", "expected"),
    [
        ("Submitted and indexed", "PASS", True),
        ("Indexed, not submitted in sitemap", "PASS", True),
        ("URL is unknown to Google", "NEUTRAL", False),
        ("Crawled - currently not indexed", "NEUTRAL", False),
        ("Discovered - currently not indexed", "NEUTRAL", False),
        # Google не сказав нічого визначеного — це НЕ «не в індексі».
        (None, "PASS", None),
        ("", "", None),
    ],
)
def test_is_indexed_state_is_three_valued(
    state: str | None, verdict: str, expected: bool | None
) -> None:
    assert is_indexed_state(state, verdict) is expected


def test_verdict_pass_alone_is_not_enough_when_state_says_not_indexed() -> None:
    """`PASS` із «not indexed» у стані не робить сторінку проіндексованою."""
    assert is_indexed_state("Crawled - currently not indexed", "PASS") is False


def test_rotation_prefers_candidates_and_fresh_submissions(
    session: Session, client_row
) -> None:
    repo = IndexingRepository(session)
    fresh = IndexingUrl(
        client_id=client_row.id,
        url="https://x.test/indexed-fresh",
        url_key="x.test/indexed-fresh",
        inspected_at=NOW,
        is_indexed=True,
    )
    candidate = IndexingUrl(
        client_id=client_row.id,
        url="https://x.test/candidate",
        url_key="x.test/candidate",
        inspected_at=NOW,
        is_indexed=False,
    )
    settled = IndexingUrl(
        client_id=client_row.id,
        url="https://x.test/settled",
        url_key="x.test/settled",
        inspected_at=NOW,
        is_indexed=True,
    )
    gone = IndexingUrl(
        client_id=client_row.id,
        url="https://x.test/gone",
        url_key="x.test/gone",
        inspected_at=NOW,
        is_indexed=False,
        gone_from_sitemap_at=NOW,
    )
    session.add_all([fresh, candidate, settled, gone])
    session.commit()

    picked = select_for_inspection(repo, client_id=client_row.id, mode="rotating", now=NOW)
    ids = [row.id for row in picked]
    assert candidate.id in ids
    # Свіжопроінспектована та проіндексована сторінка не потрібна щодня.
    assert settled.id not in ids
    # Зникла з sitemap не інспектується взагалі.
    assert gone.id not in ids


def test_rotation_picks_up_stale_rows(session: Session, client_row) -> None:
    repo = IndexingRepository(session)
    stale = IndexingUrl(
        client_id=client_row.id,
        url="https://x.test/stale",
        url_key="x.test/stale",
        inspected_at=NOW - timedelta(days=9),
        is_indexed=True,
    )
    session.add(stale)
    session.commit()
    picked = select_for_inspection(repo, client_id=client_row.id, mode="rotating", now=NOW)
    assert [row.id for row in picked] == [stale.id]


def test_full_mode_takes_everything_in_the_sitemap(session: Session, client_row) -> None:
    repo = IndexingRepository(session)
    session.add_all(
        [
            IndexingUrl(
                client_id=client_row.id,
                url=f"https://x.test/{name}",
                url_key=f"x.test/{name}",
                inspected_at=NOW,
                is_indexed=True,
            )
            for name in ("a", "b", "c")
        ]
    )
    session.commit()
    assert len(select_for_inspection(repo, client_id=client_row.id, mode="full", now=NOW)) == 3


def test_record_inspection_tracks_both_transitions(session: Session, client_row) -> None:
    """Перехід видно лише в момент порівняння станів — відновити його
    заднім числом з поточного стану неможливо."""
    repo = IndexingRepository(session)
    row = IndexingUrl(
        client_id=client_row.id, url="https://x.test/p", url_key="x.test/p"
    )
    session.add(row)
    session.commit()

    repo.record_inspection(row, fields={}, is_indexed=False, now=NOW)
    assert row.first_not_indexed_at == NOW
    assert row.first_indexed_seen_at is None

    later = NOW + timedelta(days=4)
    repo.record_inspection(row, fields={}, is_indexed=True, now=later)
    assert row.first_indexed_seen_at == later
    assert row.left_index_at is None

    dropped = later + timedelta(days=10)
    repo.record_inspection(row, fields={}, is_indexed=False, now=dropped)
    assert row.left_index_at == dropped
    # Перша поява в індексі — факт історії, його не переписують.
    assert row.first_indexed_seen_at == later


def test_unreadable_sitemap_marks_the_run_degraded() -> None:
    """Живо: у 3XE мертві 4 з 6 зареєстрованих у GSC sitemap-ів."""
    assert SyncResult().degraded is False
    assert SyncResult(errors=[{"url": "u", "error": "HTTPStatusError"}]).degraded is True


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2026-09-01", "2026-09-01"),
        ("2026-09-01T10:00:00+00:00", "2026-09-01"),
        (None, None),
        ("not a date", None),
    ],
)
def test_lastmod_parsing(raw: str | None, expected: str | None) -> None:
    parsed = _parse_lastmod(raw)
    assert (parsed.isoformat() if parsed else None) == expected
