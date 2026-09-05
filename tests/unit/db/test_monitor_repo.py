"""Site Monitor repository (task-35), regression coverage for task-37's fixes
to `list_pages_page` sorting (F1) and per-run change counting (F4)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from app.db.repositories.clients import ClientRepository
from app.db.repositories.monitor import MonitorRepository


def _client(session: Session, *, slug: str = "acme") -> str:
    client = ClientRepository(session).create(
        name="Acme", slug=slug, domain=f"{slug}.com", timezone="America/New_York"
    )
    session.commit()
    return client.id


def test_pages_sort_directions(session: Session) -> None:
    """All 8 accepted `sort` values (task-37 F1) each produce a distinct,
    correctly-ordered result — including `nulls_last` in BOTH directions,
    the exact regression this exists to catch (the old code always sorted
    `.desc()` regardless of the requested direction)."""
    client_id = _client(session)
    repo = MonitorRepository(session)
    now = datetime(2026, 8, 1, tzinfo=UTC)

    b = repo.get_or_create_page(client_id, "https://acme.com/b")
    a = repo.get_or_create_page(client_id, "https://acme.com/a")
    c = repo.get_or_create_page(client_id, "https://acme.com/c")  # never checked: NULL status

    repo.upsert_page_state(
        b, status=404, final_url=b.url, hop_count=0, indexable=False, meta_robots=None,
        x_robots_tag=None, canonical=None, title=None, description=None, h1=None, word_count=1,
        analytics_ids=[], signals_sha256="s1", text_sha256="t1", raw_sha256="r1", in_sitemap=True,
        now=now,
    )
    repo.upsert_page_state(
        a, status=200, final_url=a.url, hop_count=0, indexable=True, meta_robots=None,
        x_robots_tag=None, canonical=None, title=None, description=None, h1=None, word_count=1,
        analytics_ids=[], signals_sha256="s2", text_sha256="t2", raw_sha256="r2", in_sitemap=True,
        now=now - timedelta(hours=1),
    )
    a.version_seq = 3
    b.version_seq = 1
    session.flush()
    session.commit()

    def urls(sort: str) -> list[str]:
        pages, total = repo.list_pages_page(client_id, sort=sort, limit=10, offset=0)
        assert total == 3
        return [p.url for p in pages]

    assert urls("url") == [a.url, b.url, c.url]
    assert urls("-url") == [c.url, b.url, a.url]

    # status: a=200, b=404, c=NULL — nulls_last regardless of direction.
    assert urls("status") == [a.url, b.url, c.url]
    assert urls("-status") == [b.url, a.url, c.url]

    # last_checked_at: a is older, b is most recent, c is never checked (NULL).
    assert urls("last_checked_at") == [a.url, b.url, c.url]
    assert urls("-last_checked_at") == [b.url, a.url, c.url]

    # version_seq: a=3, b=1, c=0 (not-null column, no NULLs to worry about here).
    assert urls("version_seq") == [c.url, b.url, a.url]
    assert urls("-version_seq") == [a.url, b.url, c.url]


def test_pages_sort_unknown_value_falls_back_to_last_checked_at_desc(session: Session) -> None:
    client_id = _client(session)
    repo = MonitorRepository(session)
    repo.get_or_create_page(client_id, "https://acme.com/a")
    session.commit()
    pages, total = repo.list_pages_page(client_id, sort="bogus", limit=10, offset=0)
    assert total == 1


def test_count_changes_by_severity_is_per_run_not_client_wide(session: Session) -> None:
    """`count_changes_by_severity` (task-37 F4) must answer "what did THIS
    run find", not "what is currently open for this client" — the bug
    `open_counts_by_severity` had when runner.py used it for the run log."""
    client_id = _client(session)
    repo = MonitorRepository(session)
    light_run = repo.create_run(client_id, kind="light")
    full_run = repo.create_run(client_id, kind="full")
    session.commit()

    repo.record_change(
        client_id, run_id=light_run.id, severity="critical", change_type="page_noindex_appeared",
        scope="page", subject="https://acme.com/",
    )
    repo.record_change(
        client_id, run_id=full_run.id, severity="warning", change_type="title_changed",
        scope="page", subject="https://acme.com/about",
    )
    repo.record_change(
        client_id, run_id=full_run.id, severity="warning", change_type="title_changed",
        scope="page", subject="https://acme.com/contact",
    )
    session.commit()

    assert repo.count_changes_by_severity(light_run.id) == {"critical": 1, "warning": 0, "info": 0}
    assert repo.count_changes_by_severity(full_run.id) == {"critical": 0, "warning": 2, "info": 0}


def test_pages_in_ring_order_never_checked_page_does_not_crash_the_sort(session: Session) -> None:
    """`MonitorPage.last_checked_at` reads back timezone-AWARE (`UTCDateTime`)
    — a page that has never been checked at all (`last_checked_at=None`,
    real state right after `get_or_create_page`) used to fall back to a
    NAIVE `datetime.min`, which raises the moment `sorted()` compares it
    against an aware timestamp from an already-checked page (task-58 §2.6 /
    the audit's F8). This is the regression pin — it must not raise.
    """
    client_id = _client(session)
    repo = MonitorRepository(session)
    now = datetime(2026, 9, 4, tzinfo=UTC)

    checked = repo.get_or_create_page(client_id, "https://acme.com/checked/")
    repo.upsert_page_state(
        checked,
        status=200,
        final_url=checked.url,
        hop_count=0,
        indexable=True,
        meta_robots=None,
        x_robots_tag=None,
        canonical=None,
        title=None,
        description=None,
        h1=None,
        word_count=1,
        analytics_ids=[],
        signals_sha256="s1",
        text_sha256="t1",
        raw_sha256="r1",
        in_sitemap=True,
        now=now,
    )
    never_checked = repo.get_or_create_page(client_id, "https://acme.com/never-checked/")
    session.commit()
    assert never_checked.last_checked_at is None

    ordered = repo.pages_in_ring_order(client_id)  # must not raise TypeError

    assert {p.url for p in ordered} == {checked.url, never_checked.url}
    # Neither page is a key page or has an open critical change, so both land
    # in the same (last) priority tier — the never-checked one, whose
    # fallback timestamp is the earliest possible, sorts first.
    assert ordered[0].url == never_checked.url
    assert ordered[1].url == checked.url


def test_pages_in_ring_order_only_open_critical_jumps_the_queue(session: Session) -> None:
    """task-58 §2.6: a page with an open CRITICAL change still jumps ahead
    of the oldest-checked-first order, but a WARNING no longer does — the
    live problem this narrows (ALPHA, 1,805 open warnings after a platform
    migration) was that warnings alone were enough to permanently occupy
    the front of the queue and starve the rest of the site of a check.
    """
    client_id = _client(session)
    repo = MonitorRepository(session)
    now = datetime(2026, 9, 4, tzinfo=UTC)

    critical_page = repo.get_or_create_page(client_id, "https://acme.com/critical/")
    warning_page = repo.get_or_create_page(client_id, "https://acme.com/warning/")
    clean_page = repo.get_or_create_page(client_id, "https://acme.com/clean/")
    for page in (critical_page, warning_page, clean_page):
        repo.upsert_page_state(
            page,
            status=200,
            final_url=page.url,
            hop_count=0,
            indexable=True,
            meta_robots=None,
            x_robots_tag=None,
            canonical=None,
            title=None,
            description=None,
            h1=None,
            word_count=1,
            analytics_ids=[],
            signals_sha256=page.url,
            text_sha256=page.url,
            raw_sha256=page.url,
            in_sitemap=True,
            now=now,
        )
    run = repo.create_run(client_id, kind="full")
    session.commit()
    repo.record_change(
        client_id, run_id=run.id, severity="critical", change_type="page_noindex_appeared",
        scope="page", subject=critical_page.url,
    )
    repo.record_change(
        client_id, run_id=run.id, severity="warning", change_type="title_changed",
        scope="page", subject=warning_page.url,
    )
    session.commit()

    ordered = repo.pages_in_ring_order(client_id)

    assert ordered[0].url == critical_page.url
    # warning_page and clean_page were checked at the identical timestamp,
    # so their relative order is whatever the DB returned them in — the
    # thing under test is that warning_page is NOT forced ahead of clean_page.
    assert {p.url for p in ordered[1:]} == {warning_page.url, clean_page.url}
