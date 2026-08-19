from __future__ import annotations

from app.core.config import get_settings
from app.db.models import Client, ClientIntegration
from app.db.repositories.reports import ReportRepository
from app.reports import period_retry
from app.reports.models import PeriodMetrics
from app.reports.periods import build_monthly_periods
from app.reports.runner import period_rows
from app.schemas.enums import Provider, SourceMode

_TODAY_PERIODS = build_monthly_periods()


def _seed_client(temp_db) -> str:
    with temp_db.session_scope() as session:
        client = Client(name="EFFYS", slug="effys", domain="effys.example.com")
        session.add(client)
        session.flush()
        session.add(
            ClientIntegration(
                client_id=client.id, provider=Provider.reporting_ninja,
                source_mode=SourceMode.api,
                config_json={
                    "connection_key": "seo@x", "account_id": "properties/1",
                    "ninja_report_name": "EFFYS-Internal report",
                },
            )
        )
        session.add(
            ClientIntegration(
                client_id=client.id, provider=Provider.gsc, source_mode=SourceMode.api,
                config_json={"connection_key": "seo@x", "account_id": "sc-domain:effys.com"},
            )
        )
        return client.id


def _table_dict(*, this_month_clicks: int | None) -> dict:
    labels = [p.label for p in _TODAY_PERIODS]
    values = [
        this_month_clicks if label == "This Month" else 100 for label in labels
    ]
    return {
        "periods": [
            {
                "label": p.label, "date_from": p.date_from.isoformat(),
                "date_to": p.date_to.isoformat(), "is_partial": p.is_partial,
            }
            for p in _TODAY_PERIODS
        ],
        "change_labels": ["% M/M", "% Q/Q", "% M YOY", "% Q YOY"],
        "rows": [
            {
                "key": "clicks", "label": "Clicks", "values": list(values),
                "changes": [None, None, None, None],
            },
            {
                "key": "impressions", "label": "Impressions", "values": [200] * len(labels),
                "changes": [None, None, None, None],
            },
        ],
        "sources": {},
        "cross_check": None,
        "history": None,
    }


def _seed_partial_run(temp_db, client_id: str, *, failed_label: str = "This Month") -> str:
    rows = period_rows(_TODAY_PERIODS)
    for row in rows:
        row["state"] = "failed" if row["label"] == failed_label else "done"
    with temp_db.session_scope() as session:
        repo = ReportRepository(session)
        run = repo.create_run(
            client_id, kind="monthly", source="api", params={}, period_count=len(rows),
            periods=rows,
        )
        repo.finish(
            run.id, status="partial",
            result=_table_dict(this_month_clicks=None if failed_label == "This Month" else 8728),
        )
        return run.id


def test_rejects_unknown_run(temp_db):
    settings = get_settings()
    error = period_retry.retry_period(
        temp_db.session_scope, settings, "does-not-exist", "This Month"
    )
    assert error == "run_not_found"


def test_rejects_a_non_terminal_run(temp_db):
    client_id = _seed_client(temp_db)
    rows = period_rows(_TODAY_PERIODS)
    with temp_db.session_scope() as session:
        run = ReportRepository(session).create_run(
            client_id, kind="monthly", source="api", params={},
            period_count=len(rows), periods=rows,
        )
        run_id = run.id
    settings = get_settings()
    error = period_retry.retry_period(temp_db.session_scope, settings, run_id, "This Month")
    assert error == "run_not_finished"  # still "queued"


def test_rejects_an_unknown_period_label(temp_db):
    client_id = _seed_client(temp_db)
    run_id = _seed_partial_run(temp_db, client_id)
    settings = get_settings()
    error = period_retry.retry_period(temp_db.session_scope, settings, run_id, "Not A Period")
    assert error == "period_not_found"


def test_rejects_a_period_that_is_still_pending(temp_db):
    # task-49 §2.5: `done`/`cached` are retryable now ("Rebuild"), but a period
    # that hasn't reached a terminal state yet still can't be manually retried.
    client_id = _seed_client(temp_db)
    run_id = _seed_partial_run(temp_db, client_id)
    with temp_db.session_scope() as session:
        repo = ReportRepository(session)
        run = repo.get(run_id)
        # A fresh dict per changed row, not an in-place mutation of the
        # existing one — reassigning the SAME (mutated) dict objects back
        # onto a plain `JSON` column doesn't register as a change for
        # SQLAlchemy's flush to persist; a brand new dict does.
        new_rows = [
            {**row, "state": "waiting"} if row["label"] == "Last Month" else row
            for row in run.periods_json
        ]
        run.periods_json = new_rows
        session.flush()
    settings = get_settings()
    error = period_retry.retry_period(temp_db.session_scope, settings, run_id, "Last Month")
    assert error == "period_not_retryable"


def test_a_done_period_can_be_rebuilt(temp_db, monkeypatch):
    # task-49 §2.5: "Re-download this period" is now available for an
    # already-`done` period, not only a `failed` one — this is what makes a
    # period-level "Rebuild" possible.
    client_id = _seed_client(temp_db)
    run_id = _seed_partial_run(temp_db, client_id, failed_label="This Month")

    monkeypatch.setattr(period_retry, "discover_ga4_fields", lambda _ninja: {})
    monkeypatch.setattr(period_retry, "discover_gsc_fields", lambda _ninja: {})
    monkeypatch.setattr(
        period_retry, "fetch_period_metrics",
        lambda *a, **k: PeriodMetrics(clicks=99, impressions=200),
    )

    class FakeNinja:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

    monkeypatch.setattr(period_retry, "ReportingNinjaClient", lambda *a, **k: FakeNinja())
    monkeypatch.setattr(period_retry, "write_run_xlsx", lambda *a, **k: None)
    monkeypatch.setattr(period_retry, "write_run_client_pdf", lambda *a, **k: None)

    settings = get_settings()
    error = period_retry.retry_period(temp_db.session_scope, settings, run_id, "Last Month")
    assert error is None

    with temp_db.session_scope() as session:
        run = ReportRepository(session).get(run_id)
        row = next(p for p in run.periods_json if p["label"] == "Last Month")
    assert row["state"] == "done"


def test_a_cached_period_can_be_rebuilt(temp_db, monkeypatch):
    # task-49 §1.5/§2.5: a `cached` period had no retry path at all before —
    # `_refetch_pdf` already bypasses the cache, so only the gate needed to
    # change to make this reachable from the UI.
    client_id = _seed_client(temp_db)
    run_id = _seed_partial_run(temp_db, client_id, failed_label="This Month")
    with temp_db.session_scope() as session:
        repo = ReportRepository(session)
        run = repo.get(run_id)
        new_rows = [
            {**row, "state": "cached"} if row["label"] == "Last Month" else row
            for row in run.periods_json
        ]
        run.periods_json = new_rows
        session.flush()

    monkeypatch.setattr(period_retry, "discover_ga4_fields", lambda _ninja: {})
    monkeypatch.setattr(period_retry, "discover_gsc_fields", lambda _ninja: {})
    monkeypatch.setattr(
        period_retry, "fetch_period_metrics",
        lambda *a, **k: PeriodMetrics(clicks=99, impressions=200),
    )

    class FakeNinja:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

    monkeypatch.setattr(period_retry, "ReportingNinjaClient", lambda *a, **k: FakeNinja())
    monkeypatch.setattr(period_retry, "write_run_xlsx", lambda *a, **k: None)
    monkeypatch.setattr(period_retry, "write_run_client_pdf", lambda *a, **k: None)

    settings = get_settings()
    error = period_retry.retry_period(temp_db.session_scope, settings, run_id, "Last Month")
    assert error is None

    with temp_db.session_scope() as session:
        run = ReportRepository(session).get(run_id)
        row = next(p for p in run.periods_json if p["label"] == "Last Month")
    assert row["state"] == "done"  # a successful manual retry always lands on "done"


def test_successful_api_retry_patches_the_cell_and_flips_status_to_succeeded(
    temp_db, monkeypatch
):
    client_id = _seed_client(temp_db)
    run_id = _seed_partial_run(temp_db, client_id, failed_label="This Month")

    monkeypatch.setattr(period_retry, "discover_ga4_fields", lambda _ninja: {})
    monkeypatch.setattr(period_retry, "discover_gsc_fields", lambda _ninja: {})
    monkeypatch.setattr(
        period_retry, "fetch_period_metrics",
        lambda *a, **k: PeriodMetrics(clicks=8728, impressions=189658),
    )

    class FakeNinja:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

    monkeypatch.setattr(period_retry, "ReportingNinjaClient", lambda *a, **k: FakeNinja())
    monkeypatch.setattr(period_retry, "write_run_xlsx", lambda *a, **k: None)
    monkeypatch.setattr(period_retry, "write_run_client_pdf", lambda *a, **k: None)

    settings = get_settings()
    error = period_retry.retry_period(temp_db.session_scope, settings, run_id, "This Month")
    assert error is None

    with temp_db.session_scope() as session:
        run = ReportRepository(session).get(run_id)
        this_month_row = next(p for p in run.periods_json if p["label"] == "This Month")
        clicks_row = next(r for r in run.result_json["rows"] if r["key"] == "clicks")
        period_index = next(
            i for i, p in enumerate(run.result_json["periods"]) if p["label"] == "This Month"
        )

    assert this_month_row["state"] == "done"
    assert this_month_row["error"] is None
    assert clicks_row["values"][period_index] == 8728
    assert run.status == "succeeded"  # every period is now done — the last failure was fixed
    assert run.periods_failed == 0


def test_a_retry_that_fails_again_leaves_the_period_failed_with_a_fresh_error(
    temp_db, monkeypatch
):
    client_id = _seed_client(temp_db)
    run_id = _seed_partial_run(temp_db, client_id, failed_label="This Month")

    def _explode(*_a, **_k):
        raise RuntimeError("still broken")

    monkeypatch.setattr(period_retry, "discover_ga4_fields", lambda _ninja: {})
    monkeypatch.setattr(period_retry, "discover_gsc_fields", lambda _ninja: {})
    monkeypatch.setattr(period_retry, "fetch_period_metrics", _explode)

    class FakeNinja:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

    monkeypatch.setattr(period_retry, "ReportingNinjaClient", lambda *a, **k: FakeNinja())

    settings = get_settings()
    error = period_retry.retry_period(temp_db.session_scope, settings, run_id, "This Month")
    assert error is None  # not a request error — the period just stays failed

    with temp_db.session_scope() as session:
        run = ReportRepository(session).get(run_id)
        this_month_row = next(p for p in run.periods_json if p["label"] == "This Month")

    assert this_month_row["state"] == "failed"
    assert "still broken" in this_month_row["error"]
    assert run.status == "partial"  # unchanged — the retry itself failed
