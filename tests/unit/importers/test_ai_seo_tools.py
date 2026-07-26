from __future__ import annotations

import json
from pathlib import Path

from openpyxl import Workbook
from sqlalchemy import select

from app.db.models import (
    Client,
    ClientIntegration,
    ImportedSource,
    MetricValue,
    ProviderSnapshot,
    SyncJob,
    SyncJobProvider,
)
from app.importers.ai_seo_tools import run_import


def _write_ahrefs_artifact(source: Path, domain: str, run_date: str, **overrides: object) -> Path:
    payload = {
        "domain": domain,
        "project_id": 1,
        "audit_date": run_date,
        "crawl_date": f"{run_date}T10:00:00Z",
        "health_score": 90,
        "totals": {"urls_crawled": 100, "errors": 1, "warnings": 2},
        "verify_summary": {"total": 100},
    }
    payload.update(overrides)
    path = source / "out" / f"{domain}_{run_date}_ahrefs" / "ahrefs_findings_machine.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload))
    return path


def _write_clients_json(source: Path, data: dict) -> None:
    source.mkdir(parents=True, exist_ok=True)
    (source / "clients.json").write_text(json.dumps(data))


def _write_client_report_artifact(
    source: Path, code: str, run_date: str, historical_rows: list[tuple]
) -> Path:
    wb = Workbook()
    wb.remove(wb.active)
    ws = wb.create_sheet("Historical Data")
    ws.append([f"{code} — Monthly History"])  # real samples have a title row above the header
    ws.append(["Date", "Clicks", "Impressions", "CTR", "Organic Conversions"])
    for row in historical_rows:
        ws.append(list(row))
    path = source / "out" / f"client_report_{code}_{run_date}.xlsx"
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
    return path


def test_dry_run_writes_nothing(tmp_path, session_factory):
    source = tmp_path / "aiseotools"
    _write_ahrefs_artifact(source, "acme.com", "2026-06-01")

    report = run_import(source=source, dry_run=True, session_factory=session_factory)
    assert report.imported_count == 1
    assert report.artifacts[0].metrics_count == 4

    with session_factory() as session:
        assert session.scalars(select(Client)).all() == []
        assert session.scalars(select(ProviderSnapshot)).all() == []
        assert session.scalars(select(ImportedSource)).all() == []


def test_import_writes_client_snapshot_metrics_job_and_cell(tmp_path, session_factory):
    source = tmp_path / "aiseotools"
    _write_ahrefs_artifact(source, "acme.com", "2026-06-01")

    report = run_import(source=source, dry_run=False, session_factory=session_factory)
    assert report.imported_count == 1

    with session_factory() as session:
        [client] = session.scalars(select(Client)).all()
        assert client.domain == "acme.com"

        [snapshot] = session.scalars(select(ProviderSnapshot)).all()
        assert snapshot.client_id == client.id
        assert snapshot.provider.value == "ahrefs"

        metrics = session.scalars(
            select(MetricValue).where(MetricValue.snapshot_id == snapshot.id)
        ).all()
        assert {m.metric_key for m in metrics} == {
            "ahrefs_health_score",
            "ahrefs_errors",
            "ahrefs_warnings",
            "ahrefs_urls_crawled",
        }

        [job] = session.scalars(select(SyncJob)).all()
        assert job.mode == "imported_backfill"
        assert job.status.value == "succeeded"

        [cell] = session.scalars(select(SyncJobProvider)).all()
        assert cell.status.value == "succeeded"
        assert cell.job_id == job.id
        assert cell.client_id == client.id

        [imported] = session.scalars(select(ImportedSource)).all()
        assert imported.importer_version == "ahrefs_findings_machine.v1"
        assert imported.source_path == "out/acme.com_2026-06-01_ahrefs/ahrefs_findings_machine.json"


def test_idempotent_second_run_adds_nothing(tmp_path, session_factory):
    source = tmp_path / "aiseotools"
    _write_ahrefs_artifact(source, "acme.com", "2026-06-01")

    run_import(source=source, dry_run=False, session_factory=session_factory)
    with session_factory() as session:
        first_snapshots = len(session.scalars(select(ProviderSnapshot)).all())
        first_metrics = len(session.scalars(select(MetricValue)).all())

    report2 = run_import(source=source, dry_run=False, session_factory=session_factory)
    assert report2.artifacts[0].outcome == "skipped_already_seen"

    with session_factory() as session:
        assert len(session.scalars(select(ProviderSnapshot)).all()) == first_snapshots
        assert len(session.scalars(select(MetricValue)).all()) == first_metrics


def test_conflict_blocks_import_for_that_domain(tmp_path, session_factory):
    source = tmp_path / "aiseotools"
    _write_clients_json(
        source,
        {
            "A": {"gsc": {"account_id": "sc-domain:acme.com"}},
            "B": {"gsc": {"account_id": "https://acme.com/"}},
        },
    )
    _write_ahrefs_artifact(source, "acme.com", "2026-06-01")

    report = run_import(source=source, dry_run=True, session_factory=session_factory)
    assert report.conflicts == {"acme.com": ["A", "B"]}
    assert report.artifacts[0].outcome == "skipped_conflict"


def test_unmapped_domain_gets_a_provisional_client(tmp_path, session_factory):
    source = tmp_path / "aiseotools"
    _write_ahrefs_artifact(source, "unknown-client.com", "2026-06-01")

    run_import(source=source, dry_run=False, session_factory=session_factory)
    with session_factory() as session:
        [client] = session.scalars(select(Client)).all()
        assert client.domain == "unknown-client.com"
        assert client.name == "unknown-client.com"


def test_clients_json_code_becomes_display_name_and_syncs_integration(tmp_path, session_factory):
    source = tmp_path / "aiseotools"
    _write_clients_json(
        source,
        {"ACME": {"gsc": {"account_id": "sc-domain:acme.com", "connection_key": "seo@x.com"}}},
    )
    _write_ahrefs_artifact(source, "acme.com", "2026-06-01")

    run_import(source=source, dry_run=False, session_factory=session_factory)
    with session_factory() as session:
        [client] = session.scalars(select(Client)).all()
        assert client.name == "ACME"

        [integration] = session.scalars(select(ClientIntegration)).all()
        assert integration.provider.value == "gsc"
        assert integration.config_json["account_id"] == "sc-domain:acme.com"
        assert integration.config_json["connection_key"] == "seo@x.com"
        assert integration.source_mode.value == "api"


def test_existing_client_gets_integration_even_when_artifact_was_seen(
    tmp_path, session_factory
):
    source = tmp_path / "aiseotools"
    _write_ahrefs_artifact(source, "acme.com", "2026-06-01")
    run_import(source=source, dry_run=False, session_factory=session_factory)
    _write_clients_json(
        source,
        {"ACME": {"gsc": {"account_id": "sc-domain:acme.com", "connection_key": "seo@x.com"}}},
    )

    report = run_import(source=source, dry_run=False, session_factory=session_factory)

    assert report.artifacts[0].outcome == "skipped_already_seen"
    with session_factory() as session:
        [integration] = session.scalars(select(ClientIntegration)).all()
        assert integration.provider.value == "gsc"
        assert integration.config_json["account_id"] == "sc-domain:acme.com"


def test_import_allowlist_does_not_create_unselected_clients(tmp_path, session_factory):
    source = tmp_path / "aiseotools"
    _write_ahrefs_artifact(source, "wanted.com", "2026-06-01")
    _write_ahrefs_artifact(source, "other.com", "2026-06-01")

    report = run_import(
        source=source,
        dry_run=False,
        session_factory=session_factory,
        allowed_domains={"wanted.com"},
    )

    assert {artifact.outcome for artifact in report.artifacts} == {
        "imported",
        "skipped_not_selected",
    }
    with session_factory() as session:
        [created] = session.scalars(select(Client)).all()
        assert created.domain == "wanted.com"


def test_source_files_are_never_modified(tmp_path, session_factory):
    source = tmp_path / "aiseotools"
    path = _write_ahrefs_artifact(source, "acme.com", "2026-06-01")
    before_mtime = path.stat().st_mtime_ns
    before_bytes = path.read_bytes()

    run_import(source=source, dry_run=False, session_factory=session_factory)

    assert path.stat().st_mtime_ns == before_mtime
    assert path.read_bytes() == before_bytes


def test_one_bad_artifact_does_not_abort_the_run(tmp_path, session_factory):
    source = tmp_path / "aiseotools"
    _write_ahrefs_artifact(source, "good.com", "2026-06-01")
    bad_path = source / "out" / "bad.com_2026-06-01_ahrefs" / "ahrefs_findings_machine.json"
    bad_path.parent.mkdir(parents=True, exist_ok=True)
    bad_path.write_text("{not valid json")

    report = run_import(source=source, dry_run=False, session_factory=session_factory)
    outcomes = {a.domain_or_code: a.outcome for a in report.artifacts}
    assert outcomes["good.com"] == "imported"
    assert outcomes["bad.com"] == "skipped_recognized"

    with session_factory() as session:
        assert len(session.scalars(select(Client)).all()) == 1


def test_two_artifacts_same_domain_share_one_client(tmp_path, session_factory):
    source = tmp_path / "aiseotools"
    _write_ahrefs_artifact(source, "acme.com", "2026-06-01")
    _write_ahrefs_artifact(source, "acme.com", "2026-06-08")

    run_import(source=source, dry_run=False, session_factory=session_factory)
    with session_factory() as session:
        assert len(session.scalars(select(Client)).all()) == 1
        assert len(session.scalars(select(ProviderSnapshot)).all()) == 2


def test_multi_month_client_report_writes_one_snapshot_per_month(tmp_path, session_factory):
    """Regression test: a real client-report XLSX bundles several months of
    history in one sheet. The importer must split those into one snapshot
    per month — `metric_values` uniques on `(snapshot_id, metric_key,
    period_role)`, so cramming 14 months of `gsc_clicks` into a single
    snapshot raises a `sqlite3.IntegrityError` at write time (only caught by
    actually writing to the DB, not by the parser-level unit tests).
    """
    source = tmp_path / "aiseotools"
    _write_clients_json(source, {"ACME": {"gsc": {"account_id": "sc-domain:acme.com"}}})
    _write_client_report_artifact(
        source,
        "ACME",
        "2026-06-18",
        [
            ("May 2026", 1178, 20000, None, 50),
            ("Jun 2026", 682, 15000, None, 30),
        ],
    )

    report = run_import(source=source, dry_run=False, session_factory=session_factory)
    assert report.artifacts[0].outcome == "imported"

    with session_factory() as session:
        # 2 months x 2 providers (gsc, reporting_ninja) = 4 snapshots.
        snapshots = session.scalars(select(ProviderSnapshot)).all()
        assert len(snapshots) == 4
        for snapshot in snapshots:
            metrics = session.scalars(
                select(MetricValue).where(MetricValue.snapshot_id == snapshot.id)
            ).all()
            keys = [m.metric_key for m in metrics]
            assert len(keys) == len(set(keys)), "duplicate metric_key within one snapshot"
