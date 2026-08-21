from __future__ import annotations

import pytest

from app.contentqa import engine, policy_bridge
from app.contentqa.model import ContentDoc, ContentItem


def test_skipped_sheet_produces_info_finding_not_silence() -> None:
    doc = ContentDoc(
        source="s.xlsx", doc_type="strategy_xlsx",
        items=(), skipped_sheets=("Structure v 2.0",),
    )
    findings = engine.run_checks(doc)
    assert any(f.check == "sheet-skipped" and f.severity == "info" for f in findings)


def test_full_run_against_real_policy_finds_overlong_title() -> None:
    item = ContentItem(
        ref="row3", title="x" * 76, description="y" * 150,
        headings=(("H1", "Only Heading"),),
    )
    doc = ContentDoc(source="s.xlsx", doc_type="strategy_xlsx", items=(item,))
    findings = engine.run_checks(doc)
    assert any(f.check == "meta-title-length" and f.severity == "error" for f in findings)
    assert not any(f.check == "policy-unavailable" for f in findings)


def test_policy_unavailable_degrades_gracefully_without_crashing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom() -> None:
        raise policy_bridge.PolicyUnavailable("forced for test")

    monkeypatch.setattr(policy_bridge, "probe", _boom)

    item = ContentItem(ref="row3", title="x" * 76, headings=(("H1", "A"), ("H1", "B")))
    doc = ContentDoc(source="s.xlsx", doc_type="strategy_xlsx", items=(item,))
    findings = engine.run_checks(doc)

    assert any(f.check == "policy-unavailable" and f.severity == "warn" for f in findings)
    assert not any(f.check == "meta-title-length" for f in findings)
    assert any(f.check == "h1-multiple" for f in findings)
