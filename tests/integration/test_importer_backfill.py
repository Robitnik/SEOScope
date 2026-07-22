"""Імпорт історії AISeoTools на sanitized-фікстурах (PLAN §11, §15).

На відміну від юнітів імпортера (task-07), які будують дерево під конкретний
парсер, тут проганяється **все** закомічене дерево `tests/fixtures/aiseotools_source/`
одним запуском: dry-run, ідемпотентність, schema drift і гарантія «`--source`
лишається read-only».
"""

from __future__ import annotations

import hashlib

import pytest
from harness import build_aiseotools_source
from sqlalchemy import select

from app.db.models import Client, ClientIntegration, ImportedSource, MetricValue, ProviderSnapshot
from app.importers.ai_seo_tools import run_import
from app.schemas.enums import PeriodRole, Provider

pytestmark = pytest.mark.integration


@pytest.fixture
def source(tmp_path):
    return build_aiseotools_source(tmp_path / "aiseotools")


def _tree_digest(root):
    """Хеш усього дерева: ім'я + вміст кожного файлу."""
    digest = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        digest.update(str(path.relative_to(root)).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _outcomes(report):
    return {artifact.relative_path: artifact.outcome for artifact in report.artifacts}


def test_dry_run_writes_nothing_and_leaves_the_source_untouched(source, temp_db):
    before = _tree_digest(source)

    report = run_import(source=source, dry_run=True, session_factory=temp_db.session_factory)

    assert report.imported_count >= 3
    assert _tree_digest(source) == before, "--source зобов'язаний лишитись незмінним"
    with temp_db.session_scope() as session:
        assert session.scalars(select(Client)).all() == []
        assert session.scalars(select(ProviderSnapshot)).all() == []
        assert session.scalars(select(ImportedSource)).all() == []


def test_full_backfill_imports_every_recognized_family(source, temp_db):
    report = run_import(source=source, dry_run=False, session_factory=temp_db.session_factory)
    outcomes = _outcomes(report)

    assert outcomes["out/acme.example.com_2026-06-01_ahrefs/ahrefs_findings_machine.json"] == (
        "imported"
    )
    assert outcomes["out/acme.example.com_2026-06-08_gsc/performance_chart.csv"] == "imported"
    assert outcomes["out/beta.example.com_2026-06-08_gsc/gsc_findings_machine.json"] == "imported"
    assert outcomes["out/semrush_acme.example.com_2026-06-01.json"] == "imported"
    # Розпізнане, але свідомо не імпортоване — не помилка й не тиша.
    assert outcomes["out/acme.example.com_2026-06-01_rankmove/rank_movement_machine.json"] == (
        "skipped_recognized"
    )
    assert not any(".gsc_cache" in path for path in outcomes), "кеш-директорії не є артефактами"
    assert report.unmappable_codes == ["NO-DOMAIN-CLIENT"]

    with temp_db.session_scope() as session:
        domains = sorted(c.domain for c in session.scalars(select(Client)))
        providers = {s.provider for s in session.scalars(select(ProviderSnapshot))}
        integrations = {
            (i.provider, i.config_json.get("account_id"))
            for i in session.scalars(select(ClientIntegration))
        }

    assert domains == ["acme.example.com", "beta.example.com"]
    assert providers == {Provider.ahrefs, Provider.gsc, Provider.semrush}
    assert (Provider.gsc, "sc-domain:acme.example.com") in integrations
    assert (Provider.reporting_ninja, "properties/000000001") in integrations


def test_derived_ctr_and_missing_values_survive_the_round_trip(source, temp_db):
    run_import(source=source, dry_run=False, session_factory=temp_db.session_factory)

    with temp_db.session_scope() as session:
        acme = session.scalars(
            select(Client).where(Client.domain == "acme.example.com")
        ).one()
        rows = {
            (m.metric_key, m.period_role): m
            for m in session.scalars(
                select(MetricValue).where(
                    MetricValue.provider == Provider.gsc, MetricValue.client_id == acme.id
                )
            )
        }
        semrush = {
            m.metric_key: m
            for m in session.scalars(
                select(MetricValue).where(MetricValue.provider == Provider.semrush)
            )
        }

    clicks = rows[("gsc_clicks", PeriodRole.current)]
    assert clicks.value_numeric == 821, "клики сумуються по денних рядках performance_chart.csv"
    assert clicks.period_start.isoformat() == "2026-06-01"
    assert clicks.period_end.isoformat() == "2026-06-07"

    # `organic_traffic` у фікстурі є, `authority_score` — узятий з ascore;
    # головне, що відсутнє значення ніде не стало нулем.
    assert semrush["semrush_organic_traffic"].value_numeric == 4120
    assert all(
        m.value_numeric != 0 or m.quality.value == "complete" for m in semrush.values()
    ), "None ніколи не перетворюється на 0"


def test_second_import_is_idempotent(source, temp_db):
    first = run_import(source=source, dry_run=False, session_factory=temp_db.session_factory)
    with temp_db.session_scope() as session:
        snapshots_after_first = len(list(session.scalars(select(ProviderSnapshot))))

    second = run_import(source=source, dry_run=False, session_factory=temp_db.session_factory)

    assert first.imported_count >= 4
    assert second.imported_count == 0
    assert set(_outcomes(second).values()) == {"skipped_already_seen"}
    with temp_db.session_scope() as session:
        assert len(list(session.scalars(select(ProviderSnapshot)))) == snapshots_after_first


def test_schema_drift_is_reported_without_aborting_the_run(source, temp_db):
    """Зіпсований артефакт не валить імпорт решти (PLAN §11 «schema drift»)."""
    drifted = source / "out" / "drift.example.com_2026-06-01_ahrefs"
    drifted.mkdir(parents=True)
    (drifted / "ahrefs_findings_machine.json").write_text('{"totals": "not-an-object"}')

    report = run_import(source=source, dry_run=False, session_factory=temp_db.session_factory)
    outcomes = _outcomes(report)

    drift_key = "out/drift.example.com_2026-06-01_ahrefs/ahrefs_findings_machine.json"
    assert outcomes[drift_key] == "skipped_recognized"
    detail = next(a.detail for a in report.artifacts if a.relative_path == drift_key)
    assert "unknown schema" in detail
    assert report.imported_count >= 4, "решта артефактів мають імпортуватись попри дрейф"


def test_invalid_json_is_skipped_not_raised(source, temp_db):
    broken = source / "out" / "broken.example.com_2026-06-01_ahrefs"
    broken.mkdir(parents=True)
    (broken / "ahrefs_findings_machine.json").write_text("{ this is not json")

    report = run_import(source=source, dry_run=False, session_factory=temp_db.session_factory)

    broken_key = "out/broken.example.com_2026-06-01_ahrefs/ahrefs_findings_machine.json"
    detail = next(a.detail for a in report.artifacts if a.relative_path == broken_key)
    assert "invalid JSON" in detail or "unreadable" in detail
