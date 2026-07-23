"""`TechAuditAdapter` (task-25): crawl mode, recheck mode, cancel, failure paths.

The AISeoTools crawl/recheck are replaced at the module's lazy seams, so nothing
here touches the network, a browser, or the `seo_audit` package.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from types import SimpleNamespace

import pytest

from app.integrations import tech_audit as adapter_module
from app.integrations.tech_audit import TechAuditAdapter, finding_key
from app.jobs.fake_adapter import CancelledByAdapter
from app.schemas.adapter import CollectionContext, DateRange
from app.schemas.enums import ComparisonMode, PeriodKind, Provider, Quality, SourceMode

URL = "https://tech.example.com/missing"


def _context(**overrides) -> CollectionContext:
    base = dict(
        client_id="client-1",
        provider=Provider.tech_audit,
        source_mode=SourceMode.auto,
        period_kind=PeriodKind.month,
        comparison_mode=ComparisonMode.closed,
        current=DateRange(start=date(2026, 7, 1), end=date(2026, 7, 31)),
        previous=DateRange(start=date(2026, 6, 1), end=date(2026, 6, 30)),
        timezone="America/New_York",
        job_mode="tech_audit",
        integration_config={"domain": "tech.example.com"},
    )
    base.update(overrides)
    return CollectionContext(**base)


def _crawl_result():
    return SimpleNamespace(
        domain="tech.example.com",
        client="Acme",
        crawl_date="2026-07-23",
        audit_date="2026-07-23",
        health_score=88,
        cms="WordPress",
        pages_crawled=42,
        totals={"urls_crawled": 42, "errors": 1},
        warnings=[],
        observed_at=datetime(2026, 7, 23, tzinfo=UTC),
        categories=[
            SimpleNamespace(
                category="http_errors",
                name="HTTP errors (4XX/5XX)",
                severity="Error",
                priority="High",
                url_count=1,
                finding_count=1,
                description="",
                how_to_fix="Fix or 301 the URL.",
            ),
            SimpleNamespace(
                category="orphan",
                name="Orphan pages",
                severity="Notice",
                priority="Low",
                url_count=1,
                finding_count=1,
                description="",
                how_to_fix="Add contextual internal links.",
            ),
        ],
        findings=[
            SimpleNamespace(
                slug="page_4xx",
                category="http_errors",
                name="Page returns 4XX",
                severity="Error",
                url=URL,
                current_value="",
                measured={"status": 404},
                how_to_fix="Fix or 301 the URL.",
                final_url="",
                live_status="404",
            ),
            SimpleNamespace(
                slug="orphan_page",
                category="orphan",
                name="Orphan page",
                severity="Notice",
                url="https://tech.example.com/lonely",
                current_value="",
                measured={"inbound_count": 0},
                how_to_fix="Add contextual internal links.",
                final_url="",
                live_status="200",
            ),
        ],
    )


def _noop(_event) -> None:
    return None


# --- preflight -------------------------------------------------------------


def test_preflight_not_configured_without_a_domain(monkeypatch):
    monkeypatch.setattr(adapter_module, "_seo_audit_available", lambda: True)
    result = TechAuditAdapter().preflight(_context(integration_config={}))
    assert result.ok is False
    assert result.reason_code == "not_configured"


def test_preflight_unavailable_without_aiseotools(monkeypatch):
    monkeypatch.setattr(adapter_module, "_seo_audit_available", lambda: False)
    result = TechAuditAdapter().preflight(_context())
    assert result.ok is False
    assert result.reason_code == "unavailable"


def test_preflight_ok_costs_no_units(monkeypatch):
    monkeypatch.setattr(adapter_module, "_seo_audit_available", lambda: True)
    result = TechAuditAdapter().preflight(_context())
    assert result.ok is True
    assert result.units_estimated == 0


# --- crawl -----------------------------------------------------------------


def test_collect_crawl_produces_no_metrics_and_records_the_run_dir(monkeypatch):
    captured: dict = {}

    def _fake(**kwargs):
        captured.update(kwargs)
        return _crawl_result()

    monkeypatch.setattr(adapter_module, "_collect_tech_audit", _fake)
    adapter = TechAuditAdapter()
    context = _context()

    result = adapter.collect(context, _noop, lambda: False)

    # Rendering stays off: this container has no browser binary.
    assert captured["render"] is False
    assert captured["url"] == "tech.example.com"
    assert result.quality == Quality.complete
    assert result.metrics == []  # an audit is findings, never metric values
    assert result.provenance["run_dir"].startswith("tech_audit/client-1/")


def test_collect_datasets_emits_categories_and_findings(monkeypatch):
    monkeypatch.setattr(adapter_module, "_collect_tech_audit", lambda **_: _crawl_result())
    adapter = TechAuditAdapter()
    context = _context()
    adapter.collect(context, _noop, lambda: False)

    datasets = adapter.collect_datasets(context, _noop)

    keys = {row.dataset_key for row in datasets.rows}
    assert keys == {"tech_categories", "tech_findings"}
    findings = [r for r in datasets.rows if r.dataset_key == "tech_findings"]
    assert findings[0].dimension_value == finding_key("page_4xx", URL)
    assert findings[0].metrics["measured"]["status"] == 404
    assert findings[0].metrics["recheckable"] is True
    orphan = next(r for r in findings if r.metrics["slug"] == "orphan_page")
    assert orphan.metrics["recheckable"] is False
    assert datasets.provenance["health_score"] == 88
    assert datasets.provenance["run_dir"].startswith("tech_audit/client-1/")


def test_collect_datasets_without_a_prior_collect_is_honest(monkeypatch):
    datasets = TechAuditAdapter().collect_datasets(_context(), _noop)
    assert datasets.quality == Quality.unavailable
    assert datasets.error_code == "no_payload"
    assert datasets.rows == []


def test_duplicate_finding_keys_never_abort_the_whole_run(monkeypatch):
    """`(dataset_key, dimension_value)` is UNIQUE per snapshot.

    A real crawl produced the same `(slug, url)` twice (one page belonging to two
    duplicate-title groups); the UNIQUE violation aborted the persist transaction
    and the entire run saved zero findings. The producer merges those now, but the
    adapter must also never hand the runner a colliding row — dropping the extra
    beats losing the crawl.
    """
    duped = _crawl_result()
    duped.findings.append(duped.findings[0])  # same (slug, url) twice
    monkeypatch.setattr(adapter_module, "_collect_tech_audit", lambda **_: duped)
    adapter = TechAuditAdapter()
    context = _context()
    adapter.collect(context, _noop, lambda: False)

    datasets = adapter.collect_datasets(context, _noop)

    findings = [r for r in datasets.rows if r.dataset_key == "tech_findings"]
    keys = [r.dimension_value for r in findings]
    assert len(keys) == len(set(keys)), "no colliding dimension_value reaches the DB"
    assert len(findings) == 2, "the genuine findings are still both there"
    assert [r.rank for r in findings] == [1, 2], "ranks stay dense after a drop"


def test_crawl_reaching_nothing_is_unavailable_not_zero(monkeypatch):
    empty = _crawl_result()
    empty.pages_crawled = 0
    empty.findings = []
    empty.categories = []
    monkeypatch.setattr(adapter_module, "_collect_tech_audit", lambda **_: empty)

    result = TechAuditAdapter().collect(_context(), _noop, lambda: False)

    assert result.quality == Quality.unavailable


def test_crawl_failure_never_raises(monkeypatch):
    def _boom(**_):
        raise RuntimeError("network gone")

    monkeypatch.setattr(adapter_module, "_collect_tech_audit", _boom)

    result = TechAuditAdapter().collect(_context(), _noop, lambda: False)

    assert result.quality == Quality.unavailable
    assert result.error_code == "crawl_failed"
    assert "network gone" not in str(result.warnings)  # no raw error text leaks


def test_cancel_before_start(monkeypatch):
    monkeypatch.setattr(adapter_module, "_collect_tech_audit", lambda **_: _crawl_result())
    with pytest.raises(CancelledByAdapter):
        TechAuditAdapter().collect(_context(), _noop, lambda: True)


def test_mid_crawl_cancel_becomes_cancelled_by_adapter(monkeypatch):
    class TechAuditCancelled(BaseException):
        """Stands in for the AISeoTools signal (matched by class name)."""

    def _cancel(**_):
        raise TechAuditCancelled("stopped at a crawl checkpoint")

    monkeypatch.setattr(adapter_module, "_collect_tech_audit", _cancel)

    with pytest.raises(CancelledByAdapter):
        TechAuditAdapter().collect(_context(), _noop, lambda: False)


# --- recheck ---------------------------------------------------------------


def test_recheck_without_targets_is_refused(monkeypatch):
    context = _context(job_mode="tech_audit_recheck", job_params={})
    result = TechAuditAdapter().collect(context, _noop, lambda: False)
    assert result.quality == Quality.unavailable
    assert result.error_code == "recheck_not_targeted"


def test_recheck_with_a_missing_run_dir_is_refused(monkeypatch):
    context = _context(
        job_mode="tech_audit_recheck",
        job_params={
            "run_dir": "tech_audit/client-1/gone",
            "findings": [{"slug": "page_4xx", "url": URL}],
        },
    )
    result = TechAuditAdapter().collect(context, _noop, lambda: False)
    assert result.quality == Quality.unavailable
    assert result.error_code == "run_dir_missing"


def test_recheck_writes_tech_recheck_rows(monkeypatch, tmp_path):
    run_dir = tmp_path / "tech_audit" / "client-1" / "run-1"
    run_dir.mkdir(parents=True)
    monkeypatch.setattr(
        adapter_module, "get_settings", lambda: SimpleNamespace(artifacts_dir=str(tmp_path))
    )
    monkeypatch.setattr(
        adapter_module,
        "_recheck_tech_audit",
        lambda **_: SimpleNamespace(
            rows=[
                SimpleNamespace(
                    slug="page_4xx", url=URL, name="Page returns 4XX",
                    status="fixed", detail={"live": 200},
                )
            ]
        ),
    )
    adapter = TechAuditAdapter()
    context = _context(
        job_mode="tech_audit_recheck",
        job_params={
            "run_dir": "tech_audit/client-1/run-1",
            "findings": [{"slug": "page_4xx", "url": URL}],
        },
    )

    result = adapter.collect(context, _noop, lambda: False)
    datasets = adapter.collect_datasets(context, _noop)

    assert result.quality == Quality.complete
    assert {row.dataset_key for row in datasets.rows} == {"tech_recheck"}
    row = datasets.rows[0]
    assert row.dimension_value == finding_key("page_4xx", URL)
    assert row.metrics["status"] == "fixed"
    assert row.metrics["checked_at"]


def test_recheck_skip_categories_match_aiseotools():
    """The API answers "recheckable?" from a local literal — keep it truthful."""
    aiseotools = pytest.importorskip("seo_audit.dashboard.tech_audit")
    assert adapter_module.RECHECK_SKIP_CATEGORIES == set(aiseotools.RECHECK_SKIP_FAMILIES)
