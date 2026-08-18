"""`MonitorChange.related_id` (task-38 follow-up): page-scope changes must
carry the page's id so the change feed can link straight to that page's diff
viewer. Before this, `content_changed`/`title_changed`/... rows had no
`old_value`/`new_value` AND no way to look up the page — a dead end in the
UI (task-36 diff viewer existed but was unreachable from the change feed).
"""

from __future__ import annotations

from app.core.config import Settings
from app.db.repositories.clients import ClientRepository
from app.db.repositories.monitor import MonitorRepository
from app.monitor import runner as runner_mod
from app.monitor.fetcher import FetchResult
from app.monitor.runner import run_single_page_check

BASE = "https://acme.com"


def _fetch_returning(body: bytes):
    def fetch(url, *, throttle, stats, timeout, max_body_bytes):
        stats.requests_made += 1
        return FetchResult(
            requested_url=url,
            final_url=url,
            status=200,
            hop_count=0,
            headers={"Content-Type": "text/html"},
            body=body,
            truncated_body=False,
            elapsed_ms=1,
            error=None,
        )

    return fetch


def _check(session, client, page, tmp_path, monkeypatch, body):
    monkeypatch.setattr(runner_mod, "fetch", _fetch_returning(body))
    run_single_page_check(
        session,
        client,
        page,
        settings=Settings(_env_file=None),
        artifacts_dir=tmp_path / "artifacts",
    )


def test_page_new_change_carries_the_page_id(session, tmp_path, monkeypatch):
    client = ClientRepository(session).create(name="Acme", slug="acme", domain="acme.com")
    repo = MonitorRepository(session)
    page = repo.get_or_create_page(client.id, f"{BASE}/")
    session.commit()

    _check(session, client, page, tmp_path, monkeypatch, b"<title>Hi</title><body>hi there</body>")

    changes = repo.list_changes(client.id, state="all")
    assert [c.change_type for c in changes] == ["page_new"]
    assert changes[0].scope == "page"
    assert changes[0].related_id == page.id


def test_content_changed_change_carries_the_page_id(session, tmp_path, monkeypatch):
    client = ClientRepository(session).create(name="Acme", slug="acme", domain="acme.com")
    repo = MonitorRepository(session)
    page = repo.get_or_create_page(client.id, f"{BASE}/")
    session.commit()

    _check(session, client, page, tmp_path, monkeypatch, b"<title>Hi</title><body>hi there</body>")
    new_body = b"<title>Hi</title><body>different copy</body>"
    _check(session, client, page, tmp_path, monkeypatch, new_body)

    changes = repo.list_changes(client.id, state="all", change_type="content_changed")
    assert len(changes) == 1
    assert changes[0].related_id == page.id
