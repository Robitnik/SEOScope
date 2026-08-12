"""`run_single_page_check` (task-38 §2): re-checking one URL on demand writes
a version and detects changes exactly like a full run would, touches only
that one page, and refuses a second concurrent check for the same page.
"""

from __future__ import annotations

import pytest

from app.core.config import Settings
from app.db.repositories.clients import ClientRepository
from app.db.repositories.monitor import MonitorRepository
from app.monitor import runner as runner_mod
from app.monitor.fetcher import FetchResult
from app.monitor.runner import PageCheckConflict, run_single_page_check

BASE = "https://acme.com"


def _fake_fetch(*, status=200, body=b"<html><head><title>Hi</title></head><body>hi</body></html>"):
    def fetch(url, *, throttle, stats, timeout, max_body_bytes):
        stats.requests_made += 1
        return FetchResult(
            requested_url=url,
            final_url=url,
            status=status,
            hop_count=0,
            headers={"Content-Type": "text/html"},
            body=body,
            truncated_body=False,
            elapsed_ms=1,
            error=None,
        )

    return fetch


def test_run_single_page_check_writes_a_page_kind_run_and_a_version(session, tmp_path, monkeypatch):
    client = ClientRepository(session).create(name="Acme", slug="acme", domain="acme.com")
    repo = MonitorRepository(session)
    page = repo.get_or_create_page(client.id, f"{BASE}/")
    session.commit()

    monkeypatch.setattr(runner_mod, "fetch", _fake_fetch())
    updated = run_single_page_check(
        session,
        client,
        page,
        settings=Settings(_env_file=None),
        artifacts_dir=tmp_path / "artifacts",
    )

    assert updated.status == 200
    assert updated.title == "Hi"
    assert len(repo.page_versions(page.id, limit=10)) == 1

    runs = repo.list_runs(client.id)
    assert len(runs) == 1
    assert runs[0].kind == "page"
    assert runs[0].status == "succeeded"
    assert runs[0].urls_checked == 1


def test_run_single_page_check_detects_a_real_change_like_a_full_run_would(
    session, tmp_path, monkeypatch
):
    client = ClientRepository(session).create(name="Acme", slug="acme", domain="acme.com")
    repo = MonitorRepository(session)
    page = repo.get_or_create_page(client.id, f"{BASE}/")
    session.commit()
    settings = Settings(_env_file=None)
    art = tmp_path / "artifacts"

    # Baseline: the page as it stood before the operator's fix.
    monkeypatch.setattr(
        runner_mod,
        "fetch",
        _fake_fetch(body=b"<html><head><title>A</title></head></html>"),
    )
    run_single_page_check(session, client, page, settings=settings, artifacts_dir=art)

    # The operator just added a `noindex` — re-checking must catch it.
    monkeypatch.setattr(
        runner_mod,
        "fetch",
        _fake_fetch(
            body=b'<html><head><title>B</title><meta name="robots" content="noindex"></head></html>'
        ),
    )
    run_single_page_check(session, client, page, settings=settings, artifacts_dir=art)

    changes = repo.list_changes(client.id, state="all")
    assert any(c.change_type == "page_noindex_appeared" for c in changes)
    assert len(repo.page_versions(page.id, limit=10)) == 2


def test_run_single_page_check_does_not_touch_other_pages(session, tmp_path, monkeypatch):
    client = ClientRepository(session).create(name="Acme", slug="acme", domain="acme.com")
    repo = MonitorRepository(session)
    page = repo.get_or_create_page(client.id, f"{BASE}/a")
    other = repo.get_or_create_page(client.id, f"{BASE}/b")
    session.commit()

    monkeypatch.setattr(runner_mod, "fetch", _fake_fetch())
    run_single_page_check(
        session,
        client,
        page,
        settings=Settings(_env_file=None),
        artifacts_dir=tmp_path / "artifacts",
    )

    untouched = repo.get_page_by_id(other.id)
    assert untouched.last_checked_at is None
    assert untouched.status is None


def test_run_single_page_check_rejects_a_concurrent_call_for_the_same_page(
    session, tmp_path, monkeypatch
):
    """The in-process guard a double-click / a second operator would hit —
    the API layer turns this into a 409 (task-38 §2, precedent: `claim_target`
    in task-29)."""
    client = ClientRepository(session).create(name="Acme", slug="acme", domain="acme.com")
    repo = MonitorRepository(session)
    page = repo.get_or_create_page(client.id, f"{BASE}/")
    session.commit()

    runner_mod._PAGE_CHECK_IN_FLIGHT.add(page.id)
    try:
        with pytest.raises(PageCheckConflict):
            run_single_page_check(
                session,
                client,
                page,
                settings=Settings(_env_file=None),
                artifacts_dir=tmp_path / "artifacts",
            )
    finally:
        runner_mod._PAGE_CHECK_IN_FLIGHT.discard(page.id)

    # The guard must be released even on the happy path, or every check after
    # the first would falsely conflict with itself.
    monkeypatch.setattr(runner_mod, "fetch", _fake_fetch())
    run_single_page_check(
        session,
        client,
        page,
        settings=Settings(_env_file=None),
        artifacts_dir=tmp_path / "artifacts",
    )
    assert page.id not in runner_mod._PAGE_CHECK_IN_FLIGHT
