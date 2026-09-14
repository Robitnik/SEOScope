"""`app.authorship.backfill` (task-93 plan §2 D7, §6 test list): exact key +
time window resolution against `audit_events`, and the write-guarded apply
step.

Uses the shared Postgres `session`/`engine` harness (`tests/unit/db/
conftest.py`'s fixtures, imported via a local `conftest.py` here) — the
same one `tests/unit/db/test_authorship_repository.py` uses, since this
module reads real `ReportRun`/`ReportBatch`/`ContentQaPackage`/`AuditEvent`
rows through the ORM.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from app.authorship.backfill import (
    apply_backfill,
    backfill_batch_child_report_runs,
    backfill_content_qa_packages,
    backfill_report_batches,
    backfill_standalone_report_runs,
    run_backfill_scan,
)
from app.db.models import AuditEvent, Client, ContentQaPackage, ReportBatch, ReportRun
from app.db.repositories.users import UserRepository
from app.schemas.enums import UserRole

_BASE = datetime(2026, 9, 1, 12, 0, 0, tzinfo=UTC)


def _client(session: Session, *, slug: str = "acme") -> Client:
    client = Client(name="Acme", slug=slug, domain=f"{slug}.test", timezone="UTC")
    session.add(client)
    session.flush()
    return client


def _event(
    session: Session,
    *,
    action: str,
    ts: datetime,
    actor_kind: str = "agent",
    actor_id: str | None = "agent",
    outcome: str = "success",
    duration_ms: int | None = 100,
    request: dict | None = None,
) -> AuditEvent:
    event = AuditEvent(
        ts=ts, action=action, actor_kind=actor_kind, actor_id=actor_id, outcome=outcome,
        duration_ms=duration_ms, details_json={"request": request} if request else {},
    )
    session.add(event)
    session.flush()
    return event


def _run(
    session: Session, client_id: str, *, created_at: datetime, batch_id: str | None = None
) -> ReportRun:
    run = ReportRun(
        client_id=client_id, kind="monthly", source="api", period_count=1,
        created_at=created_at, batch_id=batch_id,
    )
    session.add(run)
    session.flush()
    return run


def _package(
    session: Session, client_id: str, *, created_at: datetime, source_file_id: str
) -> ContentQaPackage:
    package = ContentQaPackage(
        client_id=client_id, source_kind="gsheet", source_name="Strategy",
        source_file_id=source_file_id, created_at=created_at,
    )
    session.add(package)
    session.flush()
    return package


# --- content_qa_packages: exact key = file_id -------------------------------


def test_package_matches_the_one_event_with_the_same_file_id(session: Session) -> None:
    client = _client(session)
    _event(
        session, action="tool.strategy_package.run", ts=_BASE + timedelta(seconds=1),
        request={"file_id": "fileA"},
    )
    # A decoy event close in time but for a DIFFERENT file — the exact key
    # must exclude it even though the window alone would include it.
    _event(
        session, action="tool.strategy_package.run", ts=_BASE + timedelta(seconds=1),
        actor_kind="user", actor_id="someone-else", request={"file_id": "fileB"},
    )
    package = _package(session, client.id, created_at=_BASE, source_file_id="fileA")
    session.commit()

    report = backfill_content_qa_packages(session)
    assert report.matched == 1
    decision = report.decisions[0]
    assert decision.resource_id == package.id
    assert decision.attribution is not None
    assert decision.attribution.kind == "agent"


def test_package_two_events_same_file_id_different_actors_is_ambiguous(session: Session) -> None:
    client = _client(session)
    _event(
        session, action="tool.strategy_package.run", ts=_BASE + timedelta(seconds=1),
        actor_kind="agent", actor_id="agent", request={"file_id": "fileA"},
    )
    _event(
        session, action="tool.strategy_package.run", ts=_BASE + timedelta(seconds=1),
        actor_kind="user", actor_id="user-1", request={"file_id": "fileA"},
    )
    _package(session, client.id, created_at=_BASE, source_file_id="fileA")
    session.commit()

    report = backfill_content_qa_packages(session)
    assert report.ambiguous == 1
    assert report.matched == 0


def test_package_two_events_same_file_id_same_actor_still_matches(session: Session) -> None:
    """The agent fired the check twice in a row (retried after a timeout,
    say) — both audit rows share one actor, so this is unambiguous."""
    client = _client(session)
    _event(
        session, action="tool.strategy_package.run", ts=_BASE + timedelta(seconds=1),
        request={"file_id": "fileA"},
    )
    _event(
        session, action="tool.strategy_package.run",
        ts=_BASE + timedelta(seconds=1, milliseconds=500),
        request={"file_id": "fileA"},
    )
    _package(session, client.id, created_at=_BASE, source_file_id="fileA")
    session.commit()

    report = backfill_content_qa_packages(session)
    assert report.matched == 1


def test_package_with_no_matching_event_is_no_evidence(session: Session) -> None:
    client = _client(session)
    _package(session, client.id, created_at=_BASE, source_file_id="fileA")
    session.commit()

    report = backfill_content_qa_packages(session)
    assert report.no_evidence == 1


def test_package_outside_the_time_window_is_no_evidence(session: Session) -> None:
    client = _client(session)
    _event(
        session, action="tool.strategy_package.run", ts=_BASE + timedelta(minutes=5),
        request={"file_id": "fileA"},
    )
    _package(session, client.id, created_at=_BASE, source_file_id="fileA")
    session.commit()

    report = backfill_content_qa_packages(session)
    assert report.no_evidence == 1


def test_package_already_attributed_is_never_rescanned(session: Session) -> None:
    client = _client(session)
    package = _package(session, client.id, created_at=_BASE, source_file_id="fileA")
    package.created_by_kind = "user"
    package.created_by_user_id = None
    session.commit()

    report = backfill_content_qa_packages(session)
    assert report.total == 0


# --- report_runs: standalone (exact key = client_id) ------------------------


def test_standalone_run_matches_by_client_id(session: Session) -> None:
    client = _client(session)
    other_client = _client(session, slug="other")
    _event(
        session, action="report.run", ts=_BASE + timedelta(seconds=1),
        request={"client_id": client.id},
    )
    # Same timing window, but a DIFFERENT client's run request — must not
    # count as a candidate for THIS client's row.
    _event(
        session, action="report.run", ts=_BASE + timedelta(seconds=1),
        actor_kind="user", actor_id="someone-else", request={"client_id": other_client.id},
    )
    run = _run(session, client.id, created_at=_BASE)
    session.commit()

    report = backfill_standalone_report_runs(session)
    assert report.matched == 1
    assert report.decisions[0].resource_id == run.id


def test_standalone_run_ignores_batch_children(session: Session) -> None:
    client = _client(session)
    batch = ReportBatch(kind="monthly", source="api", client_count=1)
    session.add(batch)
    session.flush()
    _run(session, client.id, created_at=_BASE, batch_id=batch.id)
    session.commit()

    report = backfill_standalone_report_runs(session)
    assert report.total == 0


# --- report_runs: batch children (window only) -------------------------


def test_batch_child_run_matches_by_window_alone(session: Session) -> None:
    client = _client(session)
    batch = ReportBatch(kind="monthly", source="api", client_count=1)
    session.add(batch)
    session.flush()
    _event(session, action="report.batch_start", ts=_BASE + timedelta(seconds=1))
    run = _run(session, client.id, created_at=_BASE, batch_id=batch.id)
    session.commit()

    report = backfill_batch_child_report_runs(session)
    assert report.matched == 1
    assert report.decisions[0].resource_id == run.id


# --- report_batches (window only) -------------------------------------------


def test_report_batch_matches_by_window_alone(session: Session) -> None:
    _event(session, action="report.batch_start", ts=_BASE + timedelta(seconds=1))
    batch = ReportBatch(kind="monthly", source="api", client_count=1, created_at=_BASE)
    session.add(batch)
    session.commit()

    report = backfill_report_batches(session)
    assert report.matched == 1
    assert report.decisions[0].resource_id == batch.id


# --- apply_backfill ----------------------------------------------------------


def test_apply_writes_only_matched_rows(session: Session) -> None:
    client = _client(session)
    owner = UserRepository(session).create(
        email="owner@theyovth.com", name="Owner", role=UserRole.member, password="test-password-1"
    )
    _event(
        session, action="tool.strategy_package.run", ts=_BASE + timedelta(seconds=1),
        actor_kind="user", actor_id=owner.id, request={"file_id": "fileA"},
    )
    matched_package = _package(session, client.id, created_at=_BASE, source_file_id="fileA")
    unmatched_package = _package(session, client.id, created_at=_BASE, source_file_id="fileZ")
    session.commit()

    reports = run_backfill_scan(session)
    written = apply_backfill(session, reports)
    session.commit()

    assert written == 1
    session.refresh(matched_package)
    session.refresh(unmatched_package)
    assert matched_package.created_by_kind == "user"
    assert matched_package.created_by_user_id == owner.id
    assert unmatched_package.created_by_kind is None


def test_apply_never_overwrites_a_row_attributed_since_the_scan(session: Session) -> None:
    """Race guard (plan §2 D7): something else attributed the row between
    `run_backfill_scan` and `apply_backfill` — the stale decision must not
    clobber it."""
    client = _client(session)
    _event(
        session, action="tool.strategy_package.run", ts=_BASE + timedelta(seconds=1),
        request={"file_id": "fileA"},
    )
    package = _package(session, client.id, created_at=_BASE, source_file_id="fileA")
    session.commit()

    reports = run_backfill_scan(session)
    # Simulate a live write landing in between.
    package.created_by_kind = "user"
    package.created_by_user_id = None
    session.commit()

    written = apply_backfill(session, reports)
    session.commit()
    assert written == 0
    session.refresh(package)
    assert package.created_by_kind == "user"
