"""Ендпоінти Indexing Planner (task-100 §2.4).

Скан сюди не заходить: він ходить у живий Google API, і його поведінка
перевіряється юнітами `tests/unit/indexing/`. Тут — контракт, стани й
переходи відміток.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest

BASE = "/api/v1/tools/indexing"


@pytest.fixture
def planned(api_client: Any, seed_client: Any) -> dict[str, str]:
    """Клієнт із одним кандидатом, запланованим на сьогодні."""
    from app.db.models import IndexingSubmission, IndexingUrl
    from app.db.repositories.indexing import IndexingRepository
    from app.db.session import get_sync_sessionmaker

    client_id = seed_client(name="Acme")
    maker = get_sync_sessionmaker()
    with maker() as session:
        repo = IndexingRepository(session)
        settings = repo.get_or_create_settings(client_id)
        settings.enabled = True
        url = IndexingUrl(
            client_id=client_id,
            url="https://acme.test/page",
            url_key="acme.test/page",
            inspected_at=datetime.now(UTC),
            is_indexed=False,
            coverage_state="URL is unknown to Google",
            reason_code="never_indexed",
            reason_json={"coverage_state": "URL is unknown to Google"},
            inspection_result_link="https://search.google.com/search-console/inspect?id=abc",
        )
        session.add(url)
        session.flush()
        submission = IndexingSubmission(
            client_id=client_id,
            indexing_url_id=url.id,
            planned_for=date.today(),
            status="planned",
            reason_code="never_indexed",
            reason_json={"coverage_state": "URL is unknown to Google"},
            state_at_plan="URL is unknown to Google",
        )
        session.add(submission)
        session.commit()
        return {"client_id": client_id, "url_id": url.id, "submission_id": submission.id}


def test_overview_reports_none_for_unscanned_portfolio(
    api_client: Any, seed_client: Any
) -> None:
    """Клієнт, якого жодного разу не сканували, не має показувати «0 у
    індексі»: це факт про нас, а не про його сайт (PLAN §12)."""
    seed_client(name="Acme")
    payload = api_client.get(f"{BASE}/overview").json()
    assert payload["urls_total"] is None
    assert payload["indexed"] is None
    assert payload["coverage_ratio"] is None
    assert payload["days_to_clear_queue"] is None


def test_clients_keep_unconfigured_visible(api_client: Any, seed_client: Any) -> None:
    """Зникнути зі списку гірше за чесне «Search Console isn't connected» —
    виглядає як «клієнта не існує»."""
    seed_client(name="Acme")
    payload = api_client.get(f"{BASE}/clients").json()
    assert [row["name"] for row in payload["clients"]] == ["Acme"]
    row = payload["clients"][0]
    assert row["configured"] is False
    assert row["settings"]["enabled"] is False
    assert row["settings"]["daily_quota"] == 10
    assert row["urls_total"] is None


def test_today_returns_the_plan_with_frozen_evidence(api_client: Any, planned) -> None:
    payload = api_client.get(f"{BASE}/today").json()
    assert payload["day"] == date.today().isoformat()
    group = payload["groups"][0]
    assert group["planned"] == 1
    item = group["submissions"][0]
    assert item["reason_code"] == "never_indexed"
    assert item["reason"] == {"coverage_state": "URL is unknown to Google"}
    assert item["action"] == "submit"
    assert item["gsc_link"].startswith("https://search.google.com/")


def test_mark_sent_then_undo(api_client: Any, planned) -> None:
    submission_id = planned["submission_id"]
    sent = api_client.post(f"{BASE}/submissions/{submission_id}/sent").json()
    assert sent["status"] == "sent"
    assert sent["undoable"] is True

    url = api_client.get(f"{BASE}/urls/{planned['url_id']}").json()["url"]
    assert url["submissions_count"] == 1
    assert url["cooldown_days_left"] == 90

    undone = api_client.post(f"{BASE}/submissions/{submission_id}/undo").json()
    assert undone["status"] == "planned"
    after = api_client.get(f"{BASE}/urls/{planned['url_id']}").json()["url"]
    assert after["submissions_count"] == 0
    assert after["cooldown_days_left"] is None


def test_mark_sent_twice_does_not_double_count(api_client: Any, planned) -> None:
    submission_id = planned["submission_id"]
    first = api_client.post(f"{BASE}/submissions/{submission_id}/sent").json()
    second = api_client.post(f"{BASE}/submissions/{submission_id}/sent").json()
    assert first["submitted_at"] == second["submitted_at"]
    url = api_client.get(f"{BASE}/urls/{planned['url_id']}").json()["url"]
    assert url["submissions_count"] == 1


def test_quota_blocked_leaves_no_cooldown(api_client: Any, planned) -> None:
    """GSC відмовив по квоті — сторінка має повернутись у план, а не
    зникнути на 90 днів."""
    response = api_client.post(f"{BASE}/submissions/{planned['submission_id']}/quota-blocked")
    assert response.json()["status"] == "quota_blocked"
    url = api_client.get(f"{BASE}/urls/{planned['url_id']}").json()["url"]
    assert url["cooldown_days_left"] is None
    assert url["last_submitted_at"] is None


def test_exclude_survives_and_is_visible(api_client: Any, planned) -> None:
    excluded = api_client.post(f"{BASE}/urls/{planned['url_id']}/exclude").json()
    assert excluded["excluded"] is True
    assert excluded["excluded_reason"] == "manual"

    queue = api_client.get(f"{BASE}/queue").json()
    assert queue["total"] == 0
    # Виключене не зникає — інакше помилкове виключення ніхто не помітить.
    listed = api_client.get(f"{BASE}/queue?excluded=true").json()
    assert [row["id"] for row in listed["urls"]] == [planned["url_id"]]

    api_client.post(f"{BASE}/urls/{planned['url_id']}/include")
    assert api_client.get(f"{BASE}/queue").json()["total"] == 1


def test_settings_roundtrip(api_client: Any, planned) -> None:
    response = api_client.put(
        f"{BASE}/clients/{planned['client_id']}/settings",
        json={"daily_quota": 3, "resubmit_after_days": 45},
    )
    assert response.status_code == 200
    assert response.json() == {
        "enabled": True,
        "daily_quota": 3,
        "resubmit_after_days": 45,
        "scan_mode": "rotating",
    }


def test_calendar_marks_today_frozen(api_client: Any, planned) -> None:
    payload = api_client.get(f"{BASE}/calendar?days=8").json()
    assert payload["frozen_day"] == date.today().isoformat()
    assert len(payload["days"]) == 8
    assert payload["days"][0] == payload["frozen_day"]
    cell = payload["cells"][0]
    assert cell["planned"] == 1
    assert cell["reason_counts"] == {"never_indexed": 1}


def test_analytics_is_honest_when_empty(api_client: Any, planned) -> None:
    payload = api_client.get(f"{BASE}/analytics").json()
    assert payload["control_group"]["difference_days"] is None
    assert payload["lag"]["median_days"] is None
    assert payload["funnel"]["planned"] == 1
    assert payload["funnel"]["sent"] == 0


def test_missing_ids_are_404(api_client: Any, seed_client: Any) -> None:
    seed_client(name="Acme")
    assert api_client.get(f"{BASE}/urls/nope").status_code == 404
    assert api_client.post(f"{BASE}/submissions/nope/sent").status_code == 404
    assert api_client.get(f"{BASE}/scans/nope").status_code == 404


def test_undo_window_closed_returns_409(api_client: Any, planned) -> None:
    from app.db.repositories.indexing import IndexingRepository
    from app.db.session import get_sync_sessionmaker

    api_client.post(f"{BASE}/submissions/{planned['submission_id']}/sent")
    maker = get_sync_sessionmaker()
    with maker() as session:
        repo = IndexingRepository(session)
        row = repo.get_submission(planned["submission_id"])
        assert row is not None
        row.submitted_at = datetime.now(UTC) - timedelta(hours=30)
        repo.commit()

    response = api_client.post(f"{BASE}/submissions/{planned['submission_id']}/undo")
    assert response.status_code == 409
    assert response.json()["detail"]["error_code"] == "undo_window_closed"


def test_catalog_lists_the_tool(api_client: Any) -> None:
    tools = {row["key"]: row for row in api_client.get("/api/v1/tools").json()["tools"]}
    assert tools["indexing"]["route"] == "#/tools/indexing"
    # Нічого ще не відправлено — «Not used yet», а не підроблений `0`.
    assert tools["indexing"]["stat_value"] is None
