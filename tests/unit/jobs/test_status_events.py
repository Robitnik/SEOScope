"""Регресія «Перевірка сесій зависає».

SSE-стрім `GET /sync-jobs/{id}/events` віддає **лише** рядки `job_events`, а
фронт перечитує статуси клітинок (`frontend/js/sse.js`) винятково тоді, коли
така подія прийшла. Доки єдиним писарем подій був `make_emitter` — а він
працює *всередині* `adapter.collect()` — клітинка, що вирішилась на преflight
(`not_configured`, `awaiting_auth`, freshness-`skipped`), не давала жодної
події. Потік мовчав, модалка навічно лишалась на початковому знімку
`queued`, і всі чотири провайдери показували «Перевіряємо…».

Тому кожен перехід статусу клітинки зобов'язаний лишити подію.
"""

from __future__ import annotations

from app.db.models import JobEvent
from app.db.repositories.cells import CellRepository
from app.integrations.browser_session import SessionState
from app.jobs.runner import CellHandle, run_cell
from app.schemas.enums import CellStatus, Provider


def _status_events(session_scope, job_id: str) -> list[JobEvent]:
    with session_scope() as session:
        return [
            event
            for event in session.query(JobEvent).filter(JobEvent.job_id == job_id).all()
            if (event.details_json or {}).get("cell_status")
        ]


def _run(session_scope, job_id, cell_id, client_id, provider, **kwargs):  # type: ignore[no-untyped-def]
    return run_cell(
        CellHandle(cell_id=cell_id, job_id=job_id, client_id=client_id, provider=provider),
        session_scope=session_scope,
        lease_owner="test-worker",
        **kwargs,
    )


def test_awaiting_auth_cell_emits_a_status_event(session_scope, register_fake, make_cell):
    """Головний баг: мертва сесія паркує клітинку ще до `collect()`, тож без
    цієї події фронт ніколи не дізнається, що треба показати «Потрібен вхід».
    """
    register_fake(Provider.semrush)
    job_id, cell_id, client_id = make_cell(Provider.semrush)

    status = _run(
        session_scope,
        job_id,
        cell_id,
        client_id,
        Provider.semrush,
        check_session=lambda _p: SessionState.expired,
    )

    assert status == CellStatus.awaiting_auth
    events = _status_events(session_scope, job_id)
    assert [e.details_json["cell_status"] for e in events] == ["awaiting_auth"]
    assert events[0].provider == Provider.semrush
    assert events[0].details_json["error_code"] == "auth_required"


def test_not_configured_cell_emits_a_status_event(session_scope, register_fake, make_cell):
    register_fake(Provider.ahrefs)
    job_id, cell_id, client_id = make_cell(Provider.ahrefs, integration_enabled=False)

    status = _run(session_scope, job_id, cell_id, client_id, Provider.ahrefs)

    assert status == CellStatus.not_configured
    events = _status_events(session_scope, job_id)
    assert [e.details_json["cell_status"] for e in events] == ["not_configured"]


def test_successful_cell_emits_a_final_status_event(session_scope, register_fake, make_cell):
    register_fake(Provider.ahrefs)
    job_id, cell_id, client_id = make_cell(Provider.ahrefs, scenario="success")

    status = _run(session_scope, job_id, cell_id, client_id, Provider.ahrefs)

    assert status == CellStatus.succeeded
    assert [e.details_json["cell_status"] for e in _status_events(session_scope, job_id)] == [
        "succeeded"
    ]


def test_status_event_carries_no_invented_percent(session_scope, register_fake, make_cell):
    """`percent` — це прогрес збору. Для припаркованої клітинки він невідомий,
    тож лишається `None`: підставити 0 означало б збрехати (PLAN §12, None ≠ 0).
    """
    register_fake(Provider.semrush)
    job_id, cell_id, client_id = make_cell(Provider.semrush)

    _run(
        session_scope,
        job_id,
        cell_id,
        client_id,
        Provider.semrush,
        check_session=lambda _p: SessionState.expired,
    )

    assert _status_events(session_scope, job_id)[0].percent is None


def test_status_event_never_leaks_secrets(session_scope, register_fake, make_cell):
    """Подія — останній шов перед постійним рядком у БД (PLAN §13)."""
    register_fake(Provider.semrush)
    job_id, cell_id, client_id = make_cell(Provider.semrush)

    _run(
        session_scope,
        job_id,
        cell_id,
        client_id,
        Provider.semrush,
        check_session=lambda _p: SessionState.expired,
    )

    event = _status_events(session_scope, job_id)[0]
    assert "cookie" not in event.message.lower()
    assert set(event.details_json) == {"cell_status", "error_code"}


def test_cancelled_while_parked_is_finalized_not_left_hanging(session_scope, make_cell):
    """Клітинка в `awaiting_auth` теж має бути скасовуваною: доріжка, що стоїть
    на паузі, не крутить `claim_next_cell`, тож раніше «Зупинити» по ній нічого
    не робив і job ніколи не ставав термінальним.
    """
    from app.jobs.control import cancel_run

    job_id, cell_id, _client_id = make_cell(Provider.semrush)
    with session_scope() as session:
        CellRepository(session).set_status(
            cell_id, CellStatus.awaiting_auth, error_code="auth_required"
        )

    cancel_run(session_scope, job_id=job_id)

    with session_scope() as session:
        assert CellRepository(session).get(cell_id).status == CellStatus.cancelled
