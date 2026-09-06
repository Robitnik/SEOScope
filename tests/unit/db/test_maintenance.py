"""`run_maintenance` (task-68 §1): the extracted core of the pre-existing
`db-maintenance` CLI command, now shared with `DbMaintenanceScheduler` so
the two never diverge. Real SQLite file (WAL), same fixtures as the rest
of `tests/unit/db`.
"""

from __future__ import annotations

from sqlalchemy import text

from app.db.maintenance import MaintenanceResult, run_maintenance


def test_analyze_populates_sqlite_stat1(session):
    """The whole point (task-68 §1): before this, `sqlite_stat1` was empty
    and the planner picked indexes blind — live-measured ~4000x on the
    dashboard's own hot query. `ANALYZE` needs at least one populated
    table to write any stats at all.
    """
    session.execute(text("create table t(a integer, b integer)"))
    session.execute(text("create index ix_t_a on t(a)"))
    for i in range(200):
        session.execute(text("insert into t(a, b) values (:a, :b)"), {"a": i, "b": i * 2})
    session.commit()

    # `sqlite_stat1` does not exist at all until the first `ANALYZE` ever
    # creates it — exactly the state the production database was in.
    tables = session.execute(
        text("select name from sqlite_master where name='sqlite_stat1'")
    ).fetchall()
    assert tables == []

    run_maintenance(session)

    assert session.execute(text("select count(*) from sqlite_stat1")).scalar() > 0


def test_returns_the_checkpoint_result_and_commits(session):
    session.execute(text("create table t(a integer)"))
    session.execute(text("insert into t(a) values (1)"))
    session.commit()

    result = run_maintenance(session)

    assert isinstance(result, MaintenanceResult)
    assert result.checkpoint_busy == 0  # no other writer/reader contending in this test
    # A fresh session must see the ANALYZE-committed stats — proves
    # `run_maintenance` actually committed, not left them staged.
    fresh = session.get_bind().connect()
    try:
        assert fresh.execute(text("select count(*) from sqlite_stat1")).scalar() > 0
    finally:
        fresh.close()
