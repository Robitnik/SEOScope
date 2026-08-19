from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from app.core.config import get_settings
from app.db.models import Client, ClientIntegration, ReportFile
from app.db.repositories.reports import ReportRepository
from app.reports import runner
from app.reports.history import ReportHistory
from app.reports.models import PeriodMetrics
from app.reports.periods import build_monthly_periods
from app.schemas.enums import Provider, SourceMode

_TODAY = date(2026, 6, 19)


def _seed_client(temp_db, *, with_gsc: bool = True, report_name: str | None = None) -> str:
    with temp_db.session_scope() as session:
        client = Client(name="EFFYS", slug="effys", domain="effys.example.com")
        session.add(client)
        session.flush()
        rn_config = {"connection_key": "seo@x", "account_id": "properties/1"}
        if report_name is not None:
            rn_config["ninja_report_name"] = report_name
        session.add(
            ClientIntegration(
                client_id=client.id, provider=Provider.reporting_ninja,
                source_mode=SourceMode.api, config_json=rn_config,
            )
        )
        if with_gsc:
            session.add(
                ClientIntegration(
                    client_id=client.id, provider=Provider.gsc, source_mode=SourceMode.api,
                    config_json={"connection_key": "seo@x", "account_id": "sc-domain:effys.com"},
                )
            )
        return client.id


# --- parse_and_check: "no data" is not a parse failure -----------------------


def test_parse_and_check_keeps_a_genuinely_empty_period(monkeypatch):
    # Live regression (2026-08-19): a period whose PDF is a complete, valid
    # Reporting Ninja report that legitimately has no data (task-48 §2.3's
    # no-data normalization nulls all 8 metrics) used to be discarded as a
    # parse FAILURE, the same as a broken/mismatched PDF — EFFYS's real
    # Month YOY/Quarterly YOY periods lost their whole column over this.
    empty_metrics = PeriodMetrics()
    monkeypatch.setattr(runner, "parse_pdf", lambda _path: empty_metrics)
    monkeypatch.setattr(runner, "looks_like_a_report_pdf", lambda _path: True)

    events = []
    result = runner.parse_and_check(
        Path("irrelevant.pdf"), "Month YOY", lambda *a, **k: events.append(a)
    )
    assert result is empty_metrics
    assert events[0][0] == "info"  # not "error" — this is not a failure


def test_parse_and_check_still_fails_a_pdf_with_no_report_chrome(monkeypatch):
    # The genuine failure case this must keep catching: 0 metrics found AND
    # the PDF never had the report's own page structure — a real broken
    # download or a wrong file, not an empty period.
    empty_metrics = PeriodMetrics()
    monkeypatch.setattr(runner, "parse_pdf", lambda _path: empty_metrics)
    monkeypatch.setattr(runner, "looks_like_a_report_pdf", lambda _path: False)

    events = []
    result = runner.parse_and_check(
        Path("irrelevant.pdf"), "This Month", lambda *a, **k: events.append(a)
    )
    assert result is None
    assert events[0][0] == "error"


def _create_run(temp_db, client_id: str, *, kind="monthly", source="api", params=None) -> str:
    periods = build_monthly_periods(today=_TODAY)
    with temp_db.session_scope() as session:
        run = ReportRepository(session).create_run(
            client_id, kind=kind, source=source, params=params or {}, period_count=len(periods)
        )
        return run.id


def _fake_ga4_gsc(monkeypatch, *, metrics: PeriodMetrics):
    class FakeNinjaClient:
        request_count = 3

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

    monkeypatch.setattr(runner, "ReportingNinjaClient", lambda *a, **k: FakeNinjaClient())
    monkeypatch.setattr(runner, "discover_ga4_fields", lambda _client: {"users": "u"})
    monkeypatch.setattr(runner, "discover_gsc_fields", lambda _client: {"clicks": "c"})
    monkeypatch.setattr(
        runner, "fetch_period_metrics", lambda _client, *, date_from, date_to, **_k: metrics
    )
    # The history block is its own concern (tests/unit/reports/test_history.py)
    # — stubbed here so these tests stay hermetic instead of reaching the API.
    monkeypatch.setattr(runner, "fetch_history", lambda *a, **k: ReportHistory(months=[]))


def _periods_of(temp_db, run_id: str) -> list[dict]:
    with temp_db.session_scope() as session:
        run = ReportRepository(session).get(run_id)
        return list(run.periods_json or [])


@pytest.fixture(autouse=True)
def _ninja_credentials(monkeypatch):
    monkeypatch.setenv("REPORTING_NINJA_API_KEY", "fake-api-key")
    monkeypatch.setenv("REPORTING_NINJA_EMAIL", "ops@example.com")
    monkeypatch.setenv("REPORTING_NINJA_PASSWORD", "secret")


def test_api_run_succeeds_and_stores_the_table(temp_db, monkeypatch):
    client_id = _seed_client(temp_db)
    run_id = _create_run(temp_db, client_id)
    _fake_ga4_gsc(monkeypatch, metrics=PeriodMetrics(users=10, clicks=5))

    runner.run(temp_db.session_scope, get_settings(), run_id)

    with temp_db.session_scope() as session:
        run = ReportRepository(session).get(run_id)
    assert run.status == "succeeded"
    assert run.periods_done == 6
    assert run.periods_failed == 0
    assert run.result_json["rows"][0]["values"] == [10] * 6


def test_api_run_without_gsc_emits_a_warning(temp_db, monkeypatch):
    client_id = _seed_client(temp_db, with_gsc=False)
    run_id = _create_run(temp_db, client_id)
    _fake_ga4_gsc(monkeypatch, metrics=PeriodMetrics(users=10, clicks=None))

    runner.run(temp_db.session_scope, get_settings(), run_id)

    with temp_db.session_scope() as session:
        events = ReportRepository(session).events_since(run_id, 0)
    assert any("GSC not configured" in e.message for e in events)
    with temp_db.session_scope() as session:
        run = ReportRepository(session).get(run_id)
    assert run.status == "succeeded"


def test_api_run_without_reporting_ninja_integration_is_not_configured(temp_db):
    with temp_db.session_scope() as session:
        client = Client(name="NoRN", slug="norn", domain="norn.example.com")
        session.add(client)
        session.flush()
        client_id = client.id
    run_id = _create_run(temp_db, client_id)

    runner.run(temp_db.session_scope, get_settings(), run_id)

    with temp_db.session_scope() as session:
        run = ReportRepository(session).get(run_id)
    assert run.status == "failed"
    assert run.error_code == "not_configured"


def test_api_run_without_api_key_env_var_is_not_configured(temp_db, monkeypatch):
    monkeypatch.delenv("REPORTING_NINJA_API_KEY", raising=False)
    client_id = _seed_client(temp_db)
    run_id = _create_run(temp_db, client_id)

    runner.run(temp_db.session_scope, get_settings(), run_id)

    with temp_db.session_scope() as session:
        run = ReportRepository(session).get(run_id)
    assert run.status == "failed"
    assert run.error_code == "not_configured"


def test_cancelled_before_any_period_reports_cancelled_status(temp_db, monkeypatch):
    client_id = _seed_client(temp_db)
    run_id = _create_run(temp_db, client_id)
    _fake_ga4_gsc(monkeypatch, metrics=PeriodMetrics(users=1))
    with temp_db.session_scope() as session:
        ReportRepository(session).request_cancel(run_id)

    runner.run(temp_db.session_scope, get_settings(), run_id)

    with temp_db.session_scope() as session:
        run = ReportRepository(session).get(run_id)
    assert run.status == "cancelled"
    assert run.periods_done == 0


def test_pdf_run_without_report_name_is_report_name_missing(temp_db):
    client_id = _seed_client(temp_db, report_name=None)
    run_id = _create_run(temp_db, client_id, source="pdf")

    runner.run(temp_db.session_scope, get_settings(), run_id)

    with temp_db.session_scope() as session:
        run = ReportRepository(session).get(run_id)
    assert run.status == "failed"
    assert run.error_code == "report_name_missing"
    # task-49 §1.3/§2.3: the operator can't call a REST endpoint from the
    # error banner — the message must name the client and the two actual
    # recovery actions, not a path this run's own client will never invoke.
    assert "/clients/" not in run.error_message
    assert "EFFYS" in run.error_message
    assert "Connect" in run.error_message


def test_pdf_run_reuses_a_cached_closed_period_without_touching_the_browser(temp_db, monkeypatch):
    settings = get_settings()
    client_id = _seed_client(temp_db, report_name="EFFYS-Internal report")
    periods = build_monthly_periods(today=_TODAY)
    this_month = next(p for p in periods if p.label == "This Month")

    cached_dir = settings.artifacts_dir / "reports" / "cached"
    cached_dir.mkdir(parents=True, exist_ok=True)
    cached_pdf = cached_dir / "cached.pdf"
    cached_pdf.write_bytes(b"0" * 900_000)

    with temp_db.session_scope() as session:
        session.add(
            ReportFile(
                client_id=client_id, kind="pdf",
                path=str(cached_pdf.relative_to(settings.artifacts_dir)),
                sha256="x" * 64, size_bytes=900_000, mime="application/pdf",
                period_label=this_month.label, date_from=this_month.date_from,
                date_to=this_month.date_to, source_report_name="EFFYS-Internal report",
            )
        )

    run_id = _create_run(temp_db, client_id, kind="monthly", source="pdf")

    def _explode(*_a, **_k):
        raise AssertionError("the browser must not be invoked when every period is cached")

    monkeypatch.setattr(runner.ninja_browser, "download_periods", _explode)
    monkeypatch.setattr(
        runner, "parse_pdf", lambda _path: PeriodMetrics(users=42, clicks=7, impressions=100)
    )
    # Cross-check and the history block are separate concerns (covered by
    # test_pdf_run_persists_cross_check_details below and test_history.py) —
    # keep this test hermetic instead of letting it reach a real network call.
    monkeypatch.setattr(runner, "_try_cross_check", lambda *a, **k: None)
    monkeypatch.setattr(runner, "_try_history", lambda *a, **k: None)

    # Force every OTHER (non-"This Month") period to be treated as cached too, by
    # stubbing the cache lookup — this test only cares that a fully-cached run
    # never opens a browser, not about the other 5 periods individually.
    monkeypatch.setattr(
        ReportRepository, "find_cached_file",
        lambda self, *a, **k: session_scoped_file(cached_pdf, settings),
    )

    runner.run(temp_db.session_scope, settings, run_id)

    with temp_db.session_scope() as session:
        run = ReportRepository(session).get(run_id)
        events = ReportRepository(session).events_since(run_id, 0)
    assert run.status == "succeeded"
    assert run.periods_done == 6
    assert any("Reused cached PDF" in e.message for e in events)

    # task-49 §2.6: the run's own result records which periods were reused
    # and the oldest of their original download dates — the two "successful"
    # clients in the live incident this closes had 6/6 from yesterday's PDFs
    # with nothing in the matrix to show it.
    with temp_db.session_scope() as session:
        run = ReportRepository(session).get(run_id)
        periods_json = run.periods_json
    assert sorted(run.result_json["cached_periods"]) == sorted(p.label for p in periods)
    assert run.result_json["oldest_cached_at"] is not None
    assert all(row.get("cached_at") for row in periods_json if row["state"] == "cached")


def test_pdf_run_without_any_cache_has_no_cache_fields_in_the_result(temp_db, monkeypatch):
    settings = get_settings()
    client_id = _seed_client(temp_db, report_name="EFFYS-Internal report")
    run_id = _create_run(
        temp_db, client_id, kind="monthly", source="pdf", params={"force_refresh": True}
    )

    from app.reports.ninja_browser import DownloadedPeriod

    def _fake_download(report_name, periods, artifacts_root, **kwargs):
        pdf_path = artifacts_root / "fresh.pdf"
        pdf_path.parent.mkdir(parents=True, exist_ok=True)
        pdf_path.write_bytes(b"0" * 900_000)
        return [DownloadedPeriod(period=p, pdf_path=pdf_path) for p in periods]

    monkeypatch.setattr(runner.ninja_browser, "download_periods", _fake_download)
    monkeypatch.setattr(
        runner, "parse_pdf", lambda _path: PeriodMetrics(users=42, clicks=7, impressions=100)
    )
    monkeypatch.setattr(runner, "_try_cross_check", lambda *a, **k: None)
    monkeypatch.setattr(runner, "_try_history", lambda *a, **k: None)

    runner.run(temp_db.session_scope, settings, run_id)

    with temp_db.session_scope() as session:
        run = ReportRepository(session).get(run_id)
    assert run.status == "succeeded"
    assert "cached_periods" not in run.result_json
    assert "oldest_cached_at" not in run.result_json


def test_pdf_run_persists_cross_check_details_in_result_json(temp_db, monkeypatch):
    from app.reports.ninja_browser import DownloadedPeriod

    settings = get_settings()
    client_id = _seed_client(temp_db, report_name="EFFYS-Internal report")
    run_id = _create_run(
        temp_db, client_id, kind="monthly", source="pdf", params={"force_refresh": True}
    )

    pdf_dir = settings.artifacts_dir / "reports" / run_id
    pdf_dir.mkdir(parents=True, exist_ok=True)

    def fake_download_periods(report_name, periods, out_dir, **kwargs):
        results = []
        for period in periods:
            path = out_dir / f"{period.label}.pdf"
            path.write_bytes(b"0" * 900_000)
            results.append(DownloadedPeriod(period, path))
        return results

    monkeypatch.setattr(runner.ninja_browser, "download_periods", fake_download_periods)
    monkeypatch.setattr(runner, "parse_pdf", lambda _path: PeriodMetrics(users=3061, clicks=2528))
    _fake_ga4_gsc(monkeypatch, metrics=PeriodMetrics(users=2914, clicks=2528))  # organic drift

    runner.run(temp_db.session_scope, settings, run_id)

    with temp_db.session_scope() as session:
        run = ReportRepository(session).get(run_id)
    assert run.cross_check_status == "mismatch"
    assert run.result_json["cross_check"]["status"] == "mismatch"
    mismatches = [c for c in run.result_json["cross_check"]["cells"] if c["verdict"] == "mismatch"]
    assert any(c["metric_key"] == "users" for c in mismatches)


# --- task-45 §2.1: progress is data, not parsed log text ------------------------


def test_periods_are_listed_with_dates_before_the_first_period_runs(temp_db):
    """The enqueue path writes the whole list up front, so a `queued` run's UI
    shows `This Month · Jul 1 – Jul 31` and not `Period 1` (task-45 §1.1).
    """
    client_id = _seed_client(temp_db)
    run_id = _create_run(temp_db, client_id)

    rows = _periods_of(temp_db, run_id)
    # `_create_run` here uses the repository directly (as the scheduler tests
    # do); the API route passes `period_rows(...)`, which is what this asserts.
    with temp_db.session_scope() as session:
        ReportRepository(session).set_periods(
            run_id, runner.period_rows(build_monthly_periods(today=_TODAY))
        )
    rows = _periods_of(temp_db, run_id)
    assert [row["label"] for row in rows][0] == "This Month"
    assert all(row["state"] == "waiting" for row in rows)
    assert all(row["date_from"] and row["date_to"] for row in rows)


def test_api_run_moves_every_period_through_running_to_done(temp_db, monkeypatch):
    client_id = _seed_client(temp_db)
    run_id = _create_run(temp_db, client_id)
    seen_states: list[str] = []

    original_update = ReportRepository.update_period

    def spy(self, run_id_, label, *, state, **kwargs):
        seen_states.append(state)
        return original_update(self, run_id_, label, state=state, **kwargs)

    monkeypatch.setattr(ReportRepository, "update_period", spy)
    _fake_ga4_gsc(monkeypatch, metrics=PeriodMetrics(users=10))

    runner.run(temp_db.session_scope, get_settings(), run_id)

    # `running` is written BEFORE the work on the API path too — the exact gap
    # task-45 §1.1 found (the old code only ever emitted "<label> done").
    assert seen_states == ["running", "done"] * 6
    rows = _periods_of(temp_db, run_id)
    assert all(row["state"] == "done" for row in rows)
    assert all(row["duration_seconds"] is not None for row in rows)
    assert all(row["started_at"] and row["finished_at"] for row in rows)


def test_a_failing_period_is_recorded_as_failed_with_its_reason(temp_db, monkeypatch):
    client_id = _seed_client(temp_db)
    run_id = _create_run(temp_db, client_id)
    _fake_ga4_gsc(monkeypatch, metrics=PeriodMetrics(users=10))

    # `runner.run` resolves periods against the real today, not `_TODAY`.
    last_month = next(p for p in build_monthly_periods() if p.label == "Last Month")

    def explode_on_last_month(_client, *, date_from, date_to, **_kwargs):
        if date_from == last_month.date_from:
            raise RuntimeError("upstream said no")
        return PeriodMetrics(users=10)

    monkeypatch.setattr(runner, "fetch_period_metrics", explode_on_last_month)
    runner.run(temp_db.session_scope, get_settings(), run_id)

    rows = {row["label"]: row for row in _periods_of(temp_db, run_id)}
    assert rows["Last Month"]["state"] == "failed"
    assert "upstream said no" in rows["Last Month"]["error"]
    assert rows["This Month"]["state"] == "done"


def test_cancelling_leaves_the_remaining_periods_skipped_not_waiting(temp_db, monkeypatch):
    client_id = _seed_client(temp_db)
    run_id = _create_run(temp_db, client_id)
    _fake_ga4_gsc(monkeypatch, metrics=PeriodMetrics(users=1))
    with temp_db.session_scope() as session:
        ReportRepository(session).request_cancel(run_id)

    runner.run(temp_db.session_scope, get_settings(), run_id)

    states = {row["state"] for row in _periods_of(temp_db, run_id)}
    assert states == {"skipped"}


def test_a_run_that_cannot_start_marks_its_periods_skipped(temp_db, monkeypatch):
    monkeypatch.delenv("REPORTING_NINJA_API_KEY", raising=False)
    client_id = _seed_client(temp_db)
    run_id = _create_run(temp_db, client_id)

    runner.run(temp_db.session_scope, get_settings(), run_id)

    rows = _periods_of(temp_db, run_id)
    assert rows  # the list itself is still there, labelled
    assert {row["state"] for row in rows} == {"skipped"}


def test_a_cached_pdf_period_is_its_own_state(temp_db, monkeypatch):
    settings = get_settings()
    client_id = _seed_client(temp_db, report_name="EFFYS-Internal report")
    # Real today: the run resolves its own periods, and the cache key is the
    # period's exact date range.
    this_month = next(p for p in build_monthly_periods() if p.label == "This Month")

    cached_pdf = settings.artifacts_dir / "reports" / "cached-state" / "cached.pdf"
    cached_pdf.parent.mkdir(parents=True, exist_ok=True)
    cached_pdf.write_bytes(b"0" * 900_000)
    with temp_db.session_scope() as session:
        session.add(
            ReportFile(
                client_id=client_id, kind="pdf",
                path=str(cached_pdf.relative_to(settings.artifacts_dir)),
                sha256="y" * 64, size_bytes=900_000, mime="application/pdf",
                period_label=this_month.label, date_from=this_month.date_from,
                date_to=this_month.date_to, source_report_name="EFFYS-Internal report",
            )
        )
    run_id = _create_run(temp_db, client_id, kind="monthly", source="pdf")

    monkeypatch.setattr(runner.ninja_browser, "download_periods", lambda *a, **k: [])
    monkeypatch.setattr(runner, "parse_pdf", lambda _path: PeriodMetrics(users=42))
    monkeypatch.setattr(runner, "_try_cross_check", lambda *a, **k: None)
    monkeypatch.setattr(runner, "_try_history", lambda *a, **k: None)

    runner.run(temp_db.session_scope, settings, run_id)

    rows = {row["label"]: row for row in _periods_of(temp_db, run_id)}
    # `cached` is the difference between 0 and ~60 seconds of browser work —
    # the operator has to see which one happened (task-45 §3.1).
    assert rows["This Month"]["state"] == "cached"
    assert rows["This Month"]["from_cache"] is True


# --- task-45 §2.3: one XLSX path for both sources -------------------------------


def test_api_run_also_produces_a_downloadable_xlsx(temp_db, monkeypatch):
    client_id = _seed_client(temp_db)
    run_id = _create_run(temp_db, client_id)
    _fake_ga4_gsc(monkeypatch, metrics=PeriodMetrics(users=10, clicks=5, impressions=100))

    runner.run(temp_db.session_scope, get_settings(), run_id)

    with temp_db.session_scope() as session:
        files = ReportRepository(session).list_files_for_run(run_id)
    xlsx = [f for f in files if f.kind == "xlsx"]
    assert len(xlsx) == 1
    assert (get_settings().artifacts_dir / xlsx[0].path).is_file()
    assert xlsx[0].size_bytes > 0


def test_client_pdf_renders_only_after_the_result_is_stored(temp_db, monkeypatch):
    """Live regression (2026-08-19): the client PDF was rendered BEFORE
    `_finish` stored `result_json`, and `GET /runs/{id}/document` — the very
    URL Chromium is pointed at — answers `422 run_not_finished` until then.
    So every automatic run failed with "Failed to find element matching
    selector .report-doc__page", wrote no `client_pdf`, and left the batch's
    "Download all PDFs" archive empty. Only the manual `POST .../client-pdf`
    worked, because it necessarily runs after the run has finished.

    Pins the ordering rather than the symptom: at render time the run must
    already be readable as a finished report.
    """
    client_id = _seed_client(temp_db)
    run_id = _create_run(temp_db, client_id, kind="monthly")
    _fake_ga4_gsc(monkeypatch, metrics=PeriodMetrics(users=10, clicks=5, impressions=100))

    seen: dict[str, object] = {}

    def _fake_render(*, settings, run_id: str, out_path, headless):  # noqa: ARG001
        with temp_db.session_scope() as session:
            fresh = ReportRepository(session).get(run_id)
            seen["result_json"] = fresh.result_json
            seen["status"] = fresh.status
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(b"%PDF-1.4 fake")

    monkeypatch.setattr(runner, "render_client_pdf", _fake_render)

    runner.run(temp_db.session_scope, get_settings(), run_id)

    assert seen, "the client PDF was never rendered at all"
    assert seen["result_json"] is not None, (
        "rendered before the result was stored — /document would answer 422"
    )
    assert seen["status"] == "succeeded"

    with temp_db.session_scope() as session:
        files = ReportRepository(session).list_files_for_run(run_id)
    assert [f.kind for f in files].count("client_pdf") == 1


def test_a_run_with_no_collected_period_writes_no_xlsx(temp_db, monkeypatch):
    client_id = _seed_client(temp_db)
    run_id = _create_run(temp_db, client_id)
    _fake_ga4_gsc(monkeypatch, metrics=PeriodMetrics(users=1))
    monkeypatch.setattr(
        runner, "fetch_period_metrics",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("nope")),
    )

    runner.run(temp_db.session_scope, get_settings(), run_id)

    with temp_db.session_scope() as session:
        files = ReportRepository(session).list_files_for_run(run_id)
    assert files == []


def test_api_run_attaches_the_history_block_to_the_result(temp_db, monkeypatch):
    from app.reports.history import HistoryMonth

    client_id = _seed_client(temp_db)
    run_id = _create_run(temp_db, client_id)
    _fake_ga4_gsc(monkeypatch, metrics=PeriodMetrics(users=10))
    monkeypatch.setattr(
        runner, "fetch_history",
        lambda *a, **k: ReportHistory(
            months=[HistoryMonth("2026-07", "July 2026", 2528, 31564, 0.0801, 654)]
        ),
    )

    runner.run(temp_db.session_scope, get_settings(), run_id)

    with temp_db.session_scope() as session:
        run = ReportRepository(session).get(run_id)
    history = run.result_json["history"]
    assert history["months"][0]["organic_conversions"] == 654
    assert history["months"][0]["clicks"] == 2528


class _FakeCachedRow:
    def __init__(self, path: str, created_at):
        self.path = path
        self.created_at = created_at


def session_scoped_file(pdf_path: Path, settings):
    from datetime import UTC, datetime

    return _FakeCachedRow(str(pdf_path.relative_to(settings.artifacts_dir)), datetime.now(UTC))
