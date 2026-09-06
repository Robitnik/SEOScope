"""The freshness gate (`_is_fresh_enough` + its snapshot lookup) that decides
whether `collect_missing` skips a cell (task-76).

Regression coverage for a live incident (2026-09): a 14-day custom-window
`force_refresh` (the rolling-window preset) left a `data_through` fresher
than any later *closed week*, so the freshness gate — which only compared
dates, never ranges — treated every subsequent weekly `collect_missing` as
already covered. The week was never collected; the dashboard (which requires
an exact period match, `app/api/dashboard.py`) showed `unavailable`. Eight of
fifteen clients sat like this for about three weeks before anyone noticed.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from app.db.repositories.cells import CellRepository
from app.db.repositories.jobs import JobRepository
from app.db.repositories.metrics import MetricRepository
from app.db.repositories.snapshots import SnapshotRepository
from app.jobs.runner import CellHandle, run_cell
from app.metrics import resolve_periods
from app.schemas.adapter import MetricValueResult
from app.schemas.enums import (
    CellStatus,
    ComparisonMode,
    MetricUnit,
    PeriodKind,
    PeriodRole,
    Provider,
    Quality,
    SourceMode,
)

ANCHOR_DATE = date(2026, 1, 1)
ANCHOR_DT = datetime(2026, 1, 1, 12, tzinfo=UTC)
TZ = "America/New_York"


def _run(session_scope, job_id: str, cell_id: str, client_id: str, provider: Provider):
    handle = CellHandle(cell_id=cell_id, job_id=job_id, client_id=client_id, provider=provider)
    return run_cell(handle, session_scope=session_scope, lease_owner="w1")


def _second_job(
    session_scope,
    client_id: str,
    provider: Provider,
    *,
    period_kind: PeriodKind,
    mode: str,
    custom_start: date | None = None,
    custom_end: date | None = None,
) -> tuple[str, str]:
    """A second `SyncJob`/cell on the same already-integrated client — the
    shape of "someone ran another sync later", without `make_cell`'s
    one-client-per-call assumption.
    """
    with session_scope() as session:
        job = JobRepository(session).create_run(
            client_ids=[client_id],
            providers=[provider],
            period_kind=period_kind,
            comparison_mode=ComparisonMode.closed,
            anchor_date=ANCHOR_DATE,
            custom_start=custom_start,
            custom_end=custom_end,
            mode=mode,
        )[0]
        cell = CellRepository(session).list_for_job(job.id)[0]
        return job.id, cell.id


def test_wide_custom_refresh_does_not_block_the_next_closed_week(
    session_scope, make_cell, register_fake
):
    """The exact incident: a 14-day custom `force_refresh` must not make a
    later, narrower closed-week `collect_missing` think the week is done.
    """
    register_fake(Provider.gsc)
    week_current = resolve_periods(
        PeriodKind.week, ANCHOR_DT, tz=TZ, mode=ComparisonMode.closed
    ).current
    # A custom window that fully contains the closed week and ends after it —
    # exactly what the rolling-window preset builds.
    custom_start = week_current.start.replace(day=1)
    custom_end = week_current.end
    job1, cell1, client_id = make_cell(
        Provider.gsc,
        mode="force_refresh",
        period_kind=PeriodKind.custom,
        custom_start=custom_start,
        custom_end=custom_end,
    )
    assert _run(session_scope, job1, cell1, client_id, Provider.gsc) == CellStatus.succeeded

    job2, cell2 = _second_job(
        session_scope, client_id, Provider.gsc, period_kind=PeriodKind.week, mode="collect_missing"
    )
    status = _run(session_scope, job2, cell2, client_id, Provider.gsc)

    assert status == CellStatus.succeeded
    with session_scope() as session:
        snapshots = SnapshotRepository(session)
        current = snapshots.latest_success(
            client_id,
            Provider.gsc,
            period_start=week_current.start,
            period_end=week_current.end,
        )
        assert current is not None


def test_exact_same_week_rerun_is_still_deduped(session_scope, make_cell, register_fake):
    """Dedup must survive the fix: re-running the *same* closed week still skips."""
    register_fake(Provider.gsc)
    job1, cell1, client_id = make_cell(
        Provider.gsc, mode="force_refresh", period_kind=PeriodKind.week
    )
    assert _run(session_scope, job1, cell1, client_id, Provider.gsc) == CellStatus.succeeded

    job2, cell2 = _second_job(
        session_scope, client_id, Provider.gsc, period_kind=PeriodKind.week, mode="collect_missing"
    )
    status = _run(session_scope, job2, cell2, client_id, Provider.gsc)

    assert status == CellStatus.skipped
    with session_scope() as session:
        cell = CellRepository(session).get(cell2)
        assert cell.error_message is not None
        assert "already covers" in cell.error_message


def test_snapshot_provider_freshness_stays_period_blind(session_scope, make_cell, register_fake):
    """Ahrefs/Semrush ("snapshot" metrics, PLAN §12) are a running latest
    reading, not a reading for a period — the period-scoped lookup must not
    apply to them. A later, narrower period request still finds the wider
    custom snapshot fresh and skips.
    """
    register_fake(Provider.ahrefs)
    week_current = resolve_periods(
        PeriodKind.week, ANCHOR_DT, tz=TZ, mode=ComparisonMode.closed
    ).current
    job1, cell1, client_id = make_cell(
        Provider.ahrefs,
        mode="force_refresh",
        period_kind=PeriodKind.custom,
        custom_start=week_current.start.replace(day=1),
        custom_end=week_current.end,
    )
    assert _run(session_scope, job1, cell1, client_id, Provider.ahrefs) == CellStatus.succeeded

    job2, cell2 = _second_job(
        session_scope,
        client_id,
        Provider.ahrefs,
        period_kind=PeriodKind.week,
        mode="collect_missing",
    )
    status = _run(session_scope, job2, cell2, client_id, Provider.ahrefs)

    assert status == CellStatus.skipped


def test_incomplete_period_still_recollects(session_scope, make_cell, register_fake):
    """A snapshot that already matches the requested period's exact range but
    whose `data_through` falls short of the period's end (GSC's usual 2-3 day
    lag) must still be recollected, not treated as done.
    """
    register_fake(Provider.gsc)
    week_current = resolve_periods(
        PeriodKind.week, ANCHOR_DT, tz=TZ, mode=ComparisonMode.closed
    ).current
    job1, cell1, client_id = make_cell(
        Provider.gsc, mode="force_refresh", period_kind=PeriodKind.week
    )
    # Hand-build a same-period snapshot short of the period's end, bypassing
    # the adapter (which always reports `data_through=None`, i.e. "today") —
    # this is the one case that needs an explicit incomplete `data_through`.
    with session_scope() as session:
        snapshot = SnapshotRepository(session).create_immutable(
            job_id=job1,
            client_id=client_id,
            provider=Provider.gsc,
            source_mode=SourceMode.api,
            observed_at=datetime.now(UTC),
            quality=Quality.complete,
            data_through=week_current.start,
        )
        MetricRepository(session).bulk_insert(
            snapshot_id=snapshot.id,
            client_id=client_id,
            provider=Provider.gsc,
            values=[
                MetricValueResult(
                    metric_key="gsc_fake_metric",
                    period_role=PeriodRole.current,
                    period_start=week_current.start,
                    period_end=week_current.end,
                    value_numeric=Decimal(1),
                    unit=MetricUnit.count,
                    quality=Quality.complete,
                )
            ],
        )

    job2, cell2 = _second_job(
        session_scope, client_id, Provider.gsc, period_kind=PeriodKind.week, mode="collect_missing"
    )
    status = _run(session_scope, job2, cell2, client_id, Provider.gsc)

    assert status == CellStatus.succeeded
