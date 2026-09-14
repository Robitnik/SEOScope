"""`AuthorshipRepository` (task-93 plan §2 D3): batched author lookup and
facets across `report_runs`/`report_batches`/`content_qa_packages`.

Same shared-Postgres `session`/`engine` harness as the other repository
tests in this directory (`tests/unit/db/conftest.py`), not a bespoke
sqlite file — `harness.build_test_engine` TRUNCATEs between tests.
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app.db.models import Client, ContentQaPackage, ReportBatch, ReportRun
from app.db.repositories.authorship import (
    AuthorshipRepository,
    TooManyIdsError,
    UnknownResourceTypeError,
)
from app.db.repositories.users import UserRepository
from app.schemas.enums import UserRole


def _client(session: Session) -> Client:
    client = Client(name="Acme", slug="acme", domain="acme.test", timezone="UTC")
    session.add(client)
    session.flush()
    return client


def _user(session: Session, email: str) -> str:
    user = UserRepository(session).create(
        email=email, name=email.split("@")[0], role=UserRole.member, password="test-password-1"
    )
    return user.id


def _run(session: Session, client_id: str, **overrides: object) -> ReportRun:
    run = ReportRun(
        client_id=client_id, kind="monthly", source="api", period_count=1, **overrides
    )
    session.add(run)
    session.flush()
    return run


def test_lookup_returns_the_real_user_for_a_user_attributed_row(session: Session) -> None:
    client = _client(session)
    user_id = _user(session, "olha@theyovth.com")
    run = _run(session, client.id, created_by_kind="user", created_by_user_id=user_id)
    session.commit()

    rows = AuthorshipRepository(session).lookup("report_run", [run.id])
    assert len(rows) == 1
    assert rows[0].resource_id == run.id
    assert rows[0].author.kind == "user"
    assert rows[0].author.user is not None
    assert rows[0].author.user.id == user_id


def test_lookup_reports_no_user_for_a_dev_token_row(session: Session) -> None:
    """A dev-token/localhost row (`kind="agent"`/`"local"`) has no `users`
    row at all — the author is the kind alone, `user=None`, never a
    fabricated one."""
    client = _client(session)
    run = _run(session, client.id, created_by_kind="agent", created_by_user_id=None)
    session.commit()

    rows = AuthorshipRepository(session).lookup("report_run", [run.id])
    assert rows[0].author.kind == "agent"
    assert rows[0].author.user is None


def test_lookup_reports_kind_none_for_a_row_with_no_recorded_author(session: Session) -> None:
    """`created_by_kind IS NULL` (pre-task-93 row, or an ambiguous backfill
    candidate) — the repository hands back `kind=None`; the API layer
    turns that into the literal `"unknown"`, never a guessed value."""
    client = _client(session)
    run = _run(session, client.id)
    session.commit()

    rows = AuthorshipRepository(session).lookup("report_run", [run.id])
    assert rows[0].author.kind is None
    assert rows[0].author.user is None


def test_lookup_silently_drops_an_id_that_does_not_exist(session: Session) -> None:
    """The API's response omits an unknown id entirely — never an
    `"unknown"` author, which would misleadingly imply the resource
    exists."""
    client = _client(session)
    run = _run(session, client.id, created_by_kind="user")
    session.commit()

    rows = AuthorshipRepository(session).lookup("report_run", [run.id, "does-not-exist"])
    assert [r.resource_id for r in rows] == [run.id]


def test_lookup_rejects_an_unknown_resource_type(session: Session) -> None:
    with pytest.raises(UnknownResourceTypeError):
        AuthorshipRepository(session).lookup("client", ["x"])


def test_lookup_rejects_more_than_the_id_limit(session: Session) -> None:
    with pytest.raises(TooManyIdsError):
        AuthorshipRepository(session).lookup("report_run", [f"id-{i}" for i in range(201)])


def test_lookup_works_across_every_registered_resource_type(session: Session) -> None:
    """Not just `report_run` — `report_batch` and `content_qa_package` share
    the same two columns and the same lookup path."""
    client = _client(session)
    user_id = _user(session, "batch-owner@theyovth.com")
    batch = ReportBatch(
        kind="monthly", source="api", client_count=1,
        created_by_kind="user", created_by_user_id=user_id,
    )
    package = ContentQaPackage(
        client_id=client.id, source_kind="gsheet", source_name="Strategy",
        created_by_kind="user", created_by_user_id=user_id,
    )
    session.add_all([batch, package])
    session.commit()

    batch_rows = AuthorshipRepository(session).lookup("report_batch", [batch.id])
    package_rows = AuthorshipRepository(session).lookup("content_qa_package", [package.id])
    assert batch_rows[0].author.user is not None and batch_rows[0].author.user.id == user_id
    assert package_rows[0].author.user is not None and package_rows[0].author.user.id == user_id


def test_facets_counts_distinct_kind_and_user_pairs(session: Session) -> None:
    client = _client(session)
    alice = _user(session, "alice@theyovth.com")
    bob = _user(session, "bob@theyovth.com")
    _run(session, client.id, created_by_kind="user", created_by_user_id=alice)
    _run(session, client.id, created_by_kind="user", created_by_user_id=alice)
    _run(session, client.id, created_by_kind="user", created_by_user_id=bob)
    _run(session, client.id, created_by_kind="agent", created_by_user_id=None)
    _run(session, client.id)  # unknown (kind=None)
    session.commit()

    facets = {(f.kind, f.user.id if f.user else None): f.count for f in
              AuthorshipRepository(session).facets(["report_run"])}
    assert facets[("user", alice)] == 2
    assert facets[("user", bob)] == 1
    assert facets[("agent", None)] == 1
    assert facets[(None, None)] == 1


def test_facets_rejects_an_unknown_resource_type(session: Session) -> None:
    with pytest.raises(UnknownResourceTypeError):
        AuthorshipRepository(session).facets(["client"])
