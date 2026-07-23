"""`run_cell`: the "zero crashes" guarded boundary (PLAN §9.2/§9.3)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from app.db.repositories.analytics_datasets import AnalyticsDatasetRepository
from app.db.repositories.cells import CellRepository
from app.db.repositories.dimensions import DimensionRepository
from app.db.repositories.jobs import JobRepository
from app.db.repositories.metrics import MetricRepository
from app.db.repositories.snapshots import SnapshotRepository
from app.jobs.runner import CellHandle, run_cell
from app.schemas.enums import CellStatus, JobStatus, PeriodKind, PeriodRole, Provider, Quality


def _run(session_scope, job_id: str, cell_id: str, client_id: str, provider: Provider, **kwargs):
    handle = CellHandle(cell_id=cell_id, job_id=job_id, client_id=client_id, provider=provider)
    return run_cell(handle, session_scope=session_scope, lease_owner="w1", **kwargs)


def test_success_persists_snapshot_and_metrics_and_succeeds(
    session_scope, make_cell, register_fake
):
    register_fake(Provider.ahrefs)
    job_id, cell_id, client_id = make_cell(Provider.ahrefs, scenario="success")

    status = _run(session_scope, job_id, cell_id, client_id, Provider.ahrefs)

    assert status == CellStatus.succeeded
    with session_scope() as session:
        cell = CellRepository(session).get(cell_id)
        assert cell.status == CellStatus.succeeded
        assert cell.finished_at is not None
        job = JobRepository(session).get(job_id)
        assert job.status == JobStatus.succeeded
        assert job.progress_percent == 100.0
        snapshot = SnapshotRepository(session).latest_success(client_id, Provider.ahrefs)
        assert snapshot is not None
        # `history()` only returns current/snapshot-role rows by contract;
        # the fake adapter also emits a `previous`-role row for the same key.
        current_metrics = MetricRepository(session).history(client_id, "ahrefs_fake_metric")
        assert len(current_metrics) == 1


def test_worker_persists_provider_agnostic_ahrefs_datasets(
    session_scope, make_cell, register_fake
):
    from app.jobs.fake_adapter import FakeAdapter
    from app.schemas.analytics_datasets import (
        AnalyticsDatasetCollectionResult,
        AnalyticsDatasetValueResult,
    )

    class AhrefsDatasetFake(FakeAdapter):
        def collect_datasets(self, context, emit):
            return AnalyticsDatasetCollectionResult(
                provider=Provider.ahrefs,
                quality=Quality.complete,
                observed_at=datetime.now(UTC),
                rows=[
                    AnalyticsDatasetValueResult(
                        dataset_key="ahrefs_issues",
                        dimension_key="issue_slug",
                        dimension_value="404_page",
                        period_role=PeriodRole.current,
                        period_start=context.current.start,
                        period_end=context.current.end,
                        metrics={"name": "404 page", "severity": "Error"},
                        quality=Quality.complete,
                        rank=1,
                    )
                ],
            )

    register_fake(Provider.ahrefs, AhrefsDatasetFake(Provider.ahrefs))
    job_id, cell_id, client_id = make_cell(Provider.ahrefs, scenario="success")

    status = _run(session_scope, job_id, cell_id, client_id, Provider.ahrefs)

    assert status == CellStatus.succeeded
    with session_scope() as session:
        rows = AnalyticsDatasetRepository(session).latest(
            client_id, Provider.ahrefs, "ahrefs_issues"
        )
        assert rows[0].metrics_json["name"] == "404 page"


def test_custom_job_passes_exact_range_to_adapter(session_scope, make_cell, register_fake):
    from app.jobs.fake_adapter import FakeAdapter

    class CapturingFake(FakeAdapter):
        last_context = None

        def collect(self, context, emit, is_cancel_requested):
            self.last_context = context
            return super().collect(context, emit, is_cancel_requested)

    adapter = register_fake(Provider.gsc, CapturingFake(Provider.gsc))
    job_id, cell_id, client_id = make_cell(
        Provider.gsc,
        period_kind=PeriodKind.custom,
        custom_start=date(2025, 3, 1),
        custom_end=date(2025, 3, 31),
    )

    status = _run(session_scope, job_id, cell_id, client_id, Provider.gsc)

    assert status == CellStatus.succeeded
    assert adapter.last_context.current.start == date(2025, 3, 1)
    assert adapter.last_context.current.end == date(2025, 3, 31)
    assert adapter.last_context.previous.start == date(2025, 1, 29)
    assert adapter.last_context.previous.end == date(2025, 2, 28)


def test_partial_quality_maps_to_partial_cell_status(session_scope, make_cell, register_fake):
    register_fake(Provider.ahrefs)
    job_id, cell_id, client_id = make_cell(Provider.ahrefs, scenario="partial")

    status = _run(session_scope, job_id, cell_id, client_id, Provider.ahrefs)

    assert status == CellStatus.partial
    with session_scope() as session:
        # A lone `partial` cell still aggregates to job `succeeded` — the
        # job-level `partial` status is reserved for a *mix* of attempted
        # cells succeeding and hard-failing (PLAN §9.2), not a single cell's
        # own data-quality nuance (that nuance lives on the snapshot/metric).
        assert JobRepository(session).get(job_id).status == JobStatus.succeeded


def test_unavailable_quality_persists_snapshot_with_no_metrics(
    session_scope, make_cell, register_fake
):
    register_fake(Provider.gsc)
    job_id, cell_id, client_id = make_cell(Provider.gsc, scenario="unavailable")

    status = _run(session_scope, job_id, cell_id, client_id, Provider.gsc)

    assert status == CellStatus.partial  # attempted, no data — not a hard failure
    with session_scope() as session:
        snapshot = SnapshotRepository(session).latest_success(client_id, Provider.gsc)
        # `unavailable` is deliberately excluded from `_SUCCESS_QUALITIES`.
        assert snapshot is None


def test_gsc_worker_persists_current_and_previous_dimension_snapshots(
    session_scope, make_cell, register_fake
):
    from app.integrations.gsc import DimensionCollectionResult, DimensionValueResult
    from app.jobs.fake_adapter import FakeAdapter

    class DimensionFake(FakeAdapter):
        last_context = None

        def collect_dimensions(self, context, emit):
            self.last_context = context
            return DimensionCollectionResult(
                provider=Provider.gsc,
                quality=Quality.complete,
                observed_at=datetime.now(UTC),
                data_through=context.current.end,
                rows=[
                    DimensionValueResult(
                        dimension="page",
                        dimension_value="https://example.com/service",
                        period_role=PeriodRole.current,
                        period_start=context.current.start,
                        period_end=context.current.end,
                        clicks=12,
                        impressions=120,
                        ctr=Decimal("0.1"),
                        position=Decimal("3.5"),
                        quality=Quality.complete,
                        rank=1,
                    ),
                    DimensionValueResult(
                        dimension="page",
                        dimension_value="https://example.com/service",
                        period_role=PeriodRole.previous,
                        period_start=context.previous.start,
                        period_end=context.previous.end,
                        clicks=8,
                        impressions=100,
                        ctr=Decimal("0.08"),
                        position=Decimal("4.0"),
                        quality=Quality.complete,
                        rank=1,
                    ),
                ],
                provenance={"transport": "reporting_ninja"},
            )

    adapter = register_fake(Provider.gsc, DimensionFake(Provider.gsc))
    job_id, cell_id, client_id = make_cell(Provider.gsc)

    status = _run(session_scope, job_id, cell_id, client_id, Provider.gsc)

    assert status == CellStatus.succeeded
    context = adapter.last_context
    with session_scope() as session:
        dimensions = DimensionRepository(session)
        current, current_total = dimensions.top_pages(
            client_id, context.current.start, context.current.end
        )
        previous, previous_total = dimensions.top_pages(
            client_id, context.previous.start, context.previous.end
        )
        assert current_total == previous_total == 1
        assert current[0].clicks == 12
        assert previous[0].clicks == 8
        assert current[0].snapshot_id != previous[0].snapshot_id
        latest = SnapshotRepository(session).latest_success(client_id, Provider.gsc)
        assert latest.id == current[0].snapshot_id
        assert latest.provenance_json["dimensions"]["attempted"] is True


def test_adapter_exception_isolates_cell_as_failed_never_raises(
    session_scope, make_cell, register_fake
):
    register_fake(Provider.ahrefs)
    job_id, cell_id, client_id = make_cell(Provider.ahrefs, scenario="fail")

    status = _run(session_scope, job_id, cell_id, client_id, Provider.ahrefs)

    assert status == CellStatus.failed
    with session_scope() as session:
        cell = CellRepository(session).get(cell_id)
        assert cell.error_code == "RuntimeError"
        # message is the safe generic string, never the raw exception text
        assert "RuntimeError" != cell.error_message
        assert JobRepository(session).get(job_id).status == JobStatus.failed


def test_timeout_does_not_block_forever_and_marks_failed(session_scope, make_cell, register_fake):
    register_fake(Provider.ahrefs)
    job_id, cell_id, client_id = make_cell(Provider.ahrefs, scenario="timeout")

    status = _run(
        session_scope, job_id, cell_id, client_id, Provider.ahrefs, cell_timeout_seconds=1
    )

    assert status == CellStatus.failed
    with session_scope() as session:
        assert CellRepository(session).get(cell_id).error_code == "timeout"


def test_cancel_requested_mid_collect_marks_cell_cancelled(session_scope, make_cell, register_fake):
    register_fake(Provider.ahrefs)
    job_id, cell_id, client_id = make_cell(Provider.ahrefs, scenario="cancel")

    # Simulate a cancel request arriving while this cell is `running` — the
    # fake adapter polls `is_cancel_requested()` at its very first stage.
    with session_scope() as session:
        CellRepository(session).set_status(cell_id, CellStatus.cancel_requested)

    status = _run(session_scope, job_id, cell_id, client_id, Provider.ahrefs)

    assert status == CellStatus.cancelled


def test_missing_integration_is_not_configured_not_failed_not_crash(
    session_scope, make_cell, register_fake
):
    register_fake(Provider.semrush)
    job_id, cell_id, client_id = make_cell(Provider.semrush, no_integration=True)

    status = _run(session_scope, job_id, cell_id, client_id, Provider.semrush)

    assert status == CellStatus.not_configured
    with session_scope() as session:
        cell = CellRepository(session).get(cell_id)
        assert cell.error_code == "not_configured"
        # not_configured never counts as a failure at the job level.
        assert JobRepository(session).get(job_id).status == JobStatus.succeeded


def test_disabled_integration_is_not_configured(session_scope, make_cell, register_fake):
    register_fake(Provider.semrush)
    job_id, cell_id, client_id = make_cell(Provider.semrush, integration_enabled=False)

    status = _run(session_scope, job_id, cell_id, client_id, Provider.semrush)

    assert status == CellStatus.not_configured


def test_adapter_preflight_not_configured_reason_code_propagates(
    session_scope, make_cell, register_fake
):
    register_fake(Provider.semrush)
    job_id, cell_id, client_id = make_cell(Provider.semrush, scenario="not_configured")

    status = _run(session_scope, job_id, cell_id, client_id, Provider.semrush)

    assert status == CellStatus.not_configured
    with session_scope() as session:
        assert CellRepository(session).get(cell_id).error_code == "not_configured"


def test_unregistered_adapter_fails_cell_without_crashing(session_scope, make_cell):
    # No `register_fake` call: nothing is registered for `reporting_ninja`.
    job_id, cell_id, client_id = make_cell(Provider.reporting_ninja)

    status = _run(session_scope, job_id, cell_id, client_id, Provider.reporting_ninja)

    assert status == CellStatus.failed
    with session_scope() as session:
        assert CellRepository(session).get(cell_id).error_code == "adapter_not_registered"


def test_mixed_success_and_failure_across_a_batch_is_partial(session_scope, register_fake):
    from datetime import date

    from app.db.models import Client, ClientIntegration
    from app.schemas.enums import ComparisonMode, PeriodKind

    register_fake(Provider.ahrefs)
    with session_scope() as session:
        good = Client(name="Good", slug="good", domain="good.com", timezone="America/New_York")
        bad = Client(name="Bad", slug="bad", domain="bad.com", timezone="America/New_York")
        session.add_all([good, bad])
        session.flush()
        session.add_all(
            [
                ClientIntegration(
                    client_id=good.id,
                    provider=Provider.ahrefs,
                    enabled=True,
                    config_json={"scenario": "success"},
                ),
                ClientIntegration(
                    client_id=bad.id,
                    provider=Provider.ahrefs,
                    enabled=True,
                    config_json={"scenario": "fail"},
                ),
            ]
        )
        jobs = JobRepository(session).create_run(
            client_ids=[good.id, bad.id],
            providers=[Provider.ahrefs],
            period_kind=PeriodKind.week,
            comparison_mode=ComparisonMode.closed,
            anchor_date=date(2026, 1, 1),
        )
        batch_id = jobs[0].batch_id
        good_job_id, bad_job_id = jobs[0].id, jobs[1].id
        good_cell = CellRepository(session).list_for_job(good_job_id)[0]
        bad_cell = CellRepository(session).list_for_job(bad_job_id)[0]

    _run(session_scope, good_job_id, good_cell.id, good.id, Provider.ahrefs)
    _run(session_scope, bad_job_id, bad_cell.id, bad.id, Provider.ahrefs)

    with session_scope() as session:
        assert JobRepository(session).get(good_job_id).status == JobStatus.succeeded
        assert JobRepository(session).get(bad_job_id).status == JobStatus.failed
        assert JobRepository(session).aggregate_status(batch_id=batch_id) == JobStatus.partial
