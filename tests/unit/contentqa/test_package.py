"""contentqa/package.py — run_framework/run_package orchestration, isolation,
the min-copy-length gate (task-90 §4.6)."""

from __future__ import annotations

from typing import Any

import pytest

from app.contentqa import package as package_module
from app.contentqa import site as site_module
from app.contentqa.model import ContentDoc, ContentItem
from app.contentqa.package import (
    FrameworkOutcome,
    FrameworkSource,
    run_framework,
    run_package,
)
from app.contentqa.rules import Profile
from app.integrations.google_api.docs_client import DocsClient, DocTab
from app.monitor.fetcher import FetchStats
from app.tools.http_safety import HostThrottle


def _para(text: str) -> dict[str, object]:
    return {"paragraph": {"paragraphStyle": {}, "elements": [{"textRun": {"content": text}}]}}


def _table(*rows: tuple[list[str], list[str]]) -> dict[str, object]:
    table_rows = []
    for label_paras, value_paras in rows:
        table_rows.append({
            "tableCells": [
                {"content": [_para(t) for t in label_paras]},
                {"content": [_para(t) for t in value_paras]},
            ]
        })
    return {"table": {"tableRows": table_rows}}


def _tab(title: str, content: list[dict[str, object]]) -> DocTab:
    return DocTab(tab_id="t.0", title=title, path=(title,), body_content=tuple(content))


class _FakeDocsClient(DocsClient):
    """A real `DocsClient` subtype (not just a duck-typed lookalike) so
    mypy accepts it wherever a real one is expected — `super().__init__` is
    deliberately skipped, no real `GoogleApiSession` is ever needed."""

    def __init__(self, by_id: dict[str, tuple[DocTab, ...] | None]) -> None:
        self._by_id = by_id

    def get_document(self, document_id: str) -> tuple[DocTab, ...] | None:
        return self._by_id.get(document_id)


def _strategy(*items: ContentItem) -> ContentDoc:
    return ContentDoc(source="strategy", doc_type="strategy_gsheet", items=tuple(items))


def _common_kwargs() -> dict[str, Any]:
    return dict(
        strategy=_strategy(), profile=Profile(), throttle=HostThrottle(), stats=FetchStats(),
        check_site=False, check_published=False,
    )


# ── run_framework: honest states ──────────────────────────────────────────────


def test_not_accessible_when_document_is_none() -> None:
    source = FrameworkSource(
        document_id="missing", document_name="Missing Doc", origin="drive_search"
    )
    docs_client = _FakeDocsClient({"missing": None})
    outcome = run_framework(source, docs_client=docs_client, **_common_kwargs())
    assert outcome.state == "not_accessible"
    assert outcome.reason is not None


def test_no_recognized_tabs() -> None:
    tabs = (_tab("Draft 1", []), _tab("Draft 2", []))
    source = FrameworkSource(document_id="d1", document_name="Doc", origin="drive_search")
    docs_client = _FakeDocsClient({"d1": tabs})
    outcome = run_framework(source, docs_client=docs_client, **_common_kwargs())
    assert outcome.state == "no_recognized_tabs"


def test_checked_with_frame_tab_produces_findings() -> None:
    tabs = (_tab("Framework", [_table((["H1"], [""]))]),)  # empty H1 -> h1-missing-ish finding
    source = FrameworkSource(document_id="d1", document_name="Doc", origin="drive_search")
    docs_client = _FakeDocsClient({"d1": tabs})
    outcome = run_framework(source, docs_client=docs_client, **_common_kwargs())
    assert outcome.state == "checked"
    assert outcome.frame_tab_title == "Framework"


def test_unnamed_single_tab_gives_info_finding() -> None:
    tabs = (_tab("Tab 1", [_table((["H1"], ["Title"]))]),)
    source = FrameworkSource(document_id="d1", document_name="Doc", origin="drive_search")
    docs_client = _FakeDocsClient({"d1": tabs})
    outcome = run_framework(source, docs_client=docs_client, **_common_kwargs())
    assert outcome.state == "checked"
    assert any(f.check == "framework-tab-unnamed" for f in outcome.findings)


def test_copy_tab_without_frame_tab_gives_info_finding() -> None:
    tabs = (_tab("Content", [_para("<h1>Real Title</h1>"), _para("Some short prose.")]),)
    source = FrameworkSource(document_id="d1", document_name="Doc", origin="drive_search")
    docs_client = _FakeDocsClient({"d1": tabs})
    outcome = run_framework(source, docs_client=docs_client, **_common_kwargs())
    assert outcome.state == "checked"
    assert any(f.check == "framework-no-frame-tab" for f in outcome.findings)


def test_short_copy_tab_is_flagged_not_checked_for_prose_rules() -> None:
    tabs = (
        _tab("Framework", [_table((["H1"], ["Title"]))]),
        _tab("Content", [_para("<h1>Title</h1>"), _para("Only a short intro.")]),
    )
    source = FrameworkSource(document_id="d1", document_name="Doc", origin="drive_search")
    docs_client = _FakeDocsClient({"d1": tabs})
    outcome = run_framework(source, docs_client=docs_client, **_common_kwargs())
    assert any(f.check == "copy-too-short" for f in outcome.findings)


def test_long_copy_tab_runs_full_qa() -> None:
    long_body = " ".join(["word"] * 2000)  # comfortably over MIN_COPY_CHARS
    tabs = (
        _tab("Framework", [_table((["H1"], ["Title"]))]),
        _tab("Content", [_para("<h1>Title</h1>"), _para(long_body)]),
    )
    source = FrameworkSource(document_id="d1", document_name="Doc", origin="drive_search")
    docs_client = _FakeDocsClient({"d1": tabs})
    outcome = run_framework(source, docs_client=docs_client, **_common_kwargs())
    assert not any(f.check == "copy-too-short" for f in outcome.findings)


def test_known_row_is_passed_through_to_frame_strategy_check() -> None:
    tabs = (_tab("Framework", [_table((["H1"], ["Frame H1"]))]),)
    source = FrameworkSource(
        document_id="d1", document_name="Doc", origin="strategy_link", strategy_ref="Strategy!row 2"
    )
    docs_client = _FakeDocsClient({"d1": tabs})
    strategy_row = ContentItem(ref="Strategy!row 2", h1="Different H1")
    outcome = run_framework(
        source, docs_client=docs_client, strategy=_strategy(strategy_row), profile=Profile(),
        throttle=HostThrottle(), stats=FetchStats(), check_site=False, check_published=False,
        known_row=strategy_row,
    )
    assert outcome.frame_strategy_info is not None
    assert outcome.frame_strategy_info.state == "checked"
    assert any(f.check == "sync-h1-drift" for f in outcome.findings)


# ── run_package: fan-out, isolation, aggregate finding ────────────────────────


def test_run_package_with_no_sources_still_checks_strategy() -> None:
    strategy_row = ContentItem(ref="Strategy!row 2", url="https://example.com/a", title="X")
    result = run_package(
        _strategy(strategy_row), [], docs_client=_FakeDocsClient({}), profile=Profile()
    )
    assert result.frameworks == []
    # aggregate finding fires: no framework matched this row at all
    assert any(f.check == "sync-strategy-row-no-frame" for f in result.aggregate_findings)


def test_run_package_isolates_one_failing_framework() -> None:
    good_tabs = (_tab("Framework", [_table((["H1"], ["Title"]))]),)
    sources = [
        FrameworkSource(document_id="good", document_name="Good", origin="drive_search"),
        FrameworkSource(document_id="bad", document_name="Bad", origin="drive_search"),
    ]
    docs_client = _FakeDocsClient({"good": good_tabs, "bad": good_tabs})

    def _flaky_runner(source: FrameworkSource, **kwargs: Any) -> FrameworkOutcome:
        if source.document_id == "bad":
            raise RuntimeError("boom")
        return run_framework(source, **kwargs)

    result = run_package(
        _strategy(), sources, docs_client=docs_client, profile=Profile(),
        check_site=False, check_published=False, framework_runner=_flaky_runner,
    )
    assert len(result.frameworks) == 2
    by_id = {o.source.document_id: o for o in result.frameworks}
    assert by_id["good"].state == "checked"
    assert by_id["bad"].state == "not_accessible"
    assert "RuntimeError" in (by_id["bad"].reason or "")


def test_run_package_preserves_source_order_regardless_of_completion() -> None:
    tabs = (_tab("Framework", [_table((["H1"], ["Title"]))]),)
    sources = [
        FrameworkSource(document_id=f"d{i}", document_name=f"Doc {i}", origin="drive_search")
        for i in range(6)
    ]
    docs_client = _FakeDocsClient({s.document_id: tabs for s in sources})
    result = run_package(
        _strategy(), sources, docs_client=docs_client, profile=Profile(),
        check_site=False, check_published=False, max_workers=6,
    )
    assert [o.source.document_id for o in result.frameworks] == [f"d{i}" for i in range(6)]


def test_run_package_aggregate_finding_only_for_unmatched_rows() -> None:
    matched_row = ContentItem(ref="Strategy!row 2", url="https://example.com/matched", h1="M")
    orphan_row = ContentItem(ref="Strategy!row 3", url="https://example.com/orphan", h1="O")
    tabs = (_tab("Framework", [_table((["H1"], ["M"]))]),)
    source = FrameworkSource(
        document_id="d1", document_name="Doc", origin="strategy_link", strategy_ref="Strategy!row 2"
    )
    docs_client = _FakeDocsClient({"d1": tabs})
    result = run_package(
        _strategy(matched_row, orphan_row), [source], docs_client=docs_client, profile=Profile(),
        check_site=False, check_published=False,
    )
    unmatched_refs = {f.ref for f in result.aggregate_findings}
    assert "Strategy!row 3" in unmatched_refs
    assert "Strategy!row 2" not in unmatched_refs


# ── run_framework: site comparison wiring (monkeypatched fetch) ──────────────


def test_run_framework_wires_up_site_comparison(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.monitor.fetcher import FetchResult

    def _fake_fetch(
        url: str, *, throttle: HostThrottle, stats: FetchStats, timeout: float = 20.0
    ) -> FetchResult:
        return FetchResult(
            requested_url=url, final_url=url, status=200, hop_count=0,
            headers={"Content-Type": "text/html"},
            body=b"<html><head><title>Live Title</title></head><body><h1>H</h1></body></html>",
            truncated_body=False, elapsed_ms=1, error=None,
        )

    monkeypatch.setattr(site_module, "fetch", _fake_fetch)
    tabs = (_tab("Framework", [_table(
        (["URL"], ["https://example.com/a"]), (["H1"], ["Frame H1, not live"])
    )]),)
    source = FrameworkSource(document_id="d1", document_name="Doc", origin="drive_search")
    docs_client = _FakeDocsClient({"d1": tabs})
    outcome = run_framework(
        source, docs_client=docs_client, strategy=_strategy(), profile=Profile(),
        throttle=HostThrottle(), stats=FetchStats(), check_site=True, check_published=False,
    )
    assert outcome.frame_site_info is not None
    assert outcome.frame_site_info.state == "checked"
    assert any(f.check == "site-h1-drift" for f in outcome.findings)
    # task-91 §2.2: the raw values, not just findings about them, must
    # survive on the outcome — the comparison matrix reads these directly.
    assert outcome.frame_item is not None
    assert outcome.frame_item.url == "https://example.com/a"
    assert outcome.site_page is not None
    assert outcome.site_page.signals is not None
    assert outcome.site_page.signals.title == "Live Title"


def test_frame_item_and_site_page_are_none_without_a_frame_tab() -> None:
    tabs = (_tab("Content", [_table((["Title"], ["Copy title"]))]),)
    source = FrameworkSource(document_id="d1", document_name="Doc", origin="drive_search")
    docs_client = _FakeDocsClient({"d1": tabs})
    outcome = run_framework(source, docs_client=docs_client, **_common_kwargs())
    assert outcome.frame_item is None
    assert outcome.site_page is None


def test_site_page_stays_none_when_check_site_is_off() -> None:
    tabs = (_tab("Framework", [_table((["URL"], ["https://example.com/a"]))]),)
    source = FrameworkSource(document_id="d1", document_name="Doc", origin="drive_search")
    docs_client = _FakeDocsClient({"d1": tabs})
    outcome = run_framework(
        source, docs_client=docs_client, strategy=_strategy(), profile=Profile(),
        throttle=HostThrottle(), stats=FetchStats(), check_site=False, check_published=False,
    )
    assert outcome.frame_item is not None  # Frame tab was still read and QA'd
    assert outcome.site_page is None  # but the live site was never fetched


def test_package_module_default_runner_is_run_framework() -> None:
    """Sanity: the injectable default really is `run_framework` itself, not
    a stray copy — a change to one must not silently diverge from the
    other."""
    import inspect
    assert inspect.signature(package_module.run_package).parameters["framework_runner"].default is (
        package_module.run_framework
    )


# ── task-92 §2.5: lane K — Copy ↔ site, one CopyTabResult per copy tab ──────


def _fake_fetch_factory(html: bytes, *, status: int = 200, error: str | None = None):
    from app.monitor.fetcher import FetchResult

    def _fake_fetch(url: str, *, throttle: HostThrottle, stats: FetchStats, timeout: float = 20.0):
        return FetchResult(
            requested_url=url, final_url=url, status=status, hop_count=0,
            headers={"Content-Type": "text/html"}, body=html if error is None else None,
            truncated_body=False, elapsed_ms=1, error=error,
        )
    return _fake_fetch


def test_language_hint_matches_known_language_words() -> None:
    from app.contentqa.package import _language_hint

    assert _language_hint("English Content") == "en"
    assert _language_hint("Swedish Content") == "sv"
    assert _language_hint("SAPHNY_COPY_WEBSITE_CLIENT_HOMEPAGE") is None
    assert _language_hint("Content") is None


def test_copy_diffs_populated_for_a_finished_copy_tab(monkeypatch: pytest.MonkeyPatch) -> None:
    html = b"<html><body><h1>Title</h1><p>Real prose sentence here.</p></body></html>"
    monkeypatch.setattr(site_module, "fetch", _fake_fetch_factory(html))
    tabs = (
        _tab("Framework", [_table((["URL"], ["https://example.com/a"]))]),
        _tab("Content", [_para("<h1>Title</h1>"), _para("Real prose sentence here.")]),
    )
    source = FrameworkSource(document_id="d1", document_name="Doc", origin="drive_search")
    docs_client = _FakeDocsClient({"d1": tabs})
    outcome = run_framework(
        source, docs_client=docs_client, strategy=_strategy(), profile=Profile(),
        throttle=HostThrottle(), stats=FetchStats(), check_site=True, check_published=True,
    )
    assert len(outcome.copy_diffs) == 1
    diff = outcome.copy_diffs[0]
    assert diff.tab_title == "Content"
    assert diff.role == "primary"
    assert diff.state == "checked"
    assert diff.stats.match >= 1


def test_copy_diffs_no_url_when_framework_has_no_url() -> None:
    """task-92 §1.10/DoD #7: a framework with no URL anywhere (blog
    frameworks) must report `no_url` on each copy tab, not skip lane K
    silently and not show an empty table."""
    tabs = (
        _tab("Framework", [_table((["H1"], ["Title"]))]),  # no URL row at all
        _tab("Content", [_para("<h1>Title</h1>"), _para("Real prose sentence here.")]),
    )
    source = FrameworkSource(document_id="d1", document_name="Doc", origin="drive_search")
    docs_client = _FakeDocsClient({"d1": tabs})
    outcome = run_framework(
        source, docs_client=docs_client, strategy=_strategy(), profile=Profile(),
        throttle=HostThrottle(), stats=FetchStats(), check_site=True, check_published=True,
    )
    assert len(outcome.copy_diffs) == 1
    assert outcome.copy_diffs[0].state == "no_url"


def test_copy_diffs_not_checked_when_site_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(site_module, "fetch", _fake_fetch_factory(b"", status=403, error="blocked"))
    tabs = (
        _tab("Framework", [_table((["URL"], ["https://example.com/a"]))]),
        _tab("Content", [_para("<h1>Title</h1>"), _para("Real prose sentence here.")]),
    )
    source = FrameworkSource(document_id="d1", document_name="Doc", origin="drive_search")
    docs_client = _FakeDocsClient({"d1": tabs})
    outcome = run_framework(
        source, docs_client=docs_client, strategy=_strategy(), profile=Profile(),
        throttle=HostThrottle(), stats=FetchStats(), check_site=True, check_published=True,
    )
    assert outcome.copy_diffs[0].state == "not_checked"
    assert outcome.copy_diffs[0].reason == "blocked"


def test_copy_diffs_empty_when_check_published_is_off(monkeypatch: pytest.MonkeyPatch) -> None:
    html = b"<html><body><h1>Title</h1><p>Real prose sentence here.</p></body></html>"
    monkeypatch.setattr(site_module, "fetch", _fake_fetch_factory(html))
    tabs = (
        _tab("Framework", [_table((["URL"], ["https://example.com/a"]))]),
        _tab("Content", [_para("<h1>Title</h1>"), _para("Real prose sentence here.")]),
    )
    source = FrameworkSource(document_id="d1", document_name="Doc", origin="drive_search")
    docs_client = _FakeDocsClient({"d1": tabs})
    outcome = run_framework(
        source, docs_client=docs_client, strategy=_strategy(), profile=Profile(),
        throttle=HostThrottle(), stats=FetchStats(), check_site=True, check_published=False,
    )
    assert outcome.copy_diffs == ()


def test_copy_diffs_role_selection_by_coverage_not_order(monkeypatch: pytest.MonkeyPatch) -> None:
    """LAXTON shape (task-92 §1.8/§2.5): two language variants on one
    document — the one with HIGHER coverage against the live site must
    win `role="primary"`, regardless of which tab comes first."""
    html = (
        b'<html lang="sv-SE"><body><h1>Spirit Box</h1>'
        b"<p>Svensk text om produkten.</p></body></html>"
    )
    monkeypatch.setattr(site_module, "fetch", _fake_fetch_factory(html))
    tabs = (
        _tab("Framework", [_table((["URL"], ["https://laxton.se/p"]))]),
        _tab("English Content", [
            _para("<h1>Spirit Box</h1>"), _para("Completely different English body."),
        ]),
        _tab("Swedish Content", [
            _para("<h1>Spirit Box</h1>"), _para("Svensk text om produkten."),
        ]),
    )
    source = FrameworkSource(document_id="d1", document_name="Doc", origin="drive_search")
    docs_client = _FakeDocsClient({"d1": tabs})
    outcome = run_framework(
        source, docs_client=docs_client, strategy=_strategy(), profile=Profile(),
        throttle=HostThrottle(), stats=FetchStats(), check_site=True, check_published=True,
    )
    by_title = {d.tab_title: d for d in outcome.copy_diffs}
    assert by_title["Swedish Content"].role == "primary"
    assert by_title["English Content"].role == "secondary"


def test_copy_diffs_wrong_language_with_no_matching_page_is_not_applicable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """task-92 §1.8/§2.5: an English-language tab compared against a
    Swedish-only site with near-zero coverage reads as "no published page
    in this language", not a wall of false "missing" findings."""
    html = (
        b'<html lang="sv-SE"><body><h1>Andekontakt</h1>'
        b"<p>Helt annan svensk text.</p></body></html>"
    )
    monkeypatch.setattr(site_module, "fetch", _fake_fetch_factory(html))
    tabs = (
        _tab("Framework", [_table((["URL"], ["https://laxton.se/en/p"]))]),
        _tab("English Content", [
            _para("<h1>Spirit Box</h1>"), _para("Nothing in common with the page."),
        ]),
    )
    source = FrameworkSource(document_id="d1", document_name="Doc", origin="drive_search")
    docs_client = _FakeDocsClient({"d1": tabs})
    outcome = run_framework(
        source, docs_client=docs_client, strategy=_strategy(), profile=Profile(),
        throttle=HostThrottle(), stats=FetchStats(), check_site=True, check_published=True,
    )
    assert outcome.copy_diffs[0].state == "not_applicable"
    assert outcome.copy_diffs[0].reason == "No published page in this language."


# ── task-95 §2.3: link_audit — Framework -> Copy -> Site, wired into run_framework ──


def _linked_run(content: str, url: str | None = None) -> dict[str, object]:
    text_run: dict[str, object] = {"content": content}
    if url:
        text_run["textStyle"] = {"link": {"url": url}}
    return {"textRun": text_run}


def _linked_para(*runs: dict[str, object]) -> dict[str, object]:
    return {"paragraph": {"elements": list(runs)}}


def test_link_audit_is_not_applicable_without_a_frame_tab() -> None:
    """A Copy-only document (no Framework tab at all) still gets a
    `link_audit` — `not_applicable`, since neither side claims any link —
    never `None` for a `state="checked"` outcome."""
    tabs = (_tab("Content", [_para("<h1>Real Title</h1>"), _para("Some short prose.")]),)
    source = FrameworkSource(document_id="d1", document_name="Doc", origin="drive_search")
    docs_client = _FakeDocsClient({"d1": tabs})
    outcome = run_framework(source, docs_client=docs_client, **_common_kwargs())
    assert outcome.state == "checked"
    assert outcome.frame_item is None
    assert outcome.link_audit is not None
    assert outcome.link_audit.state == "not_applicable"


def test_link_audit_catches_framework_links_missing_from_copy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """task-95 §1.1 reproduced against `run_framework` directly: a
    Framework-declared link that never made it into the copy tab must
    read `mismatch`, regardless of whether the live site happens to carry
    that link somewhere else (nav, footer — irrelevant to this step)."""
    html = b"<html><body><h1>H</h1><p>No links in this prose at all.</p></body></html>"
    monkeypatch.setattr(site_module, "fetch", _fake_fetch_factory(html))
    tabs = (
        _tab("Framework", [_table(
            (["URL"], ["https://example.com/a"]),
            (["Internal Link + Anchor Text"], ["our menu (/menu/)"]),
        )]),
        _tab("Content", [_linked_para(_linked_run("Some unrelated prose."))]),
    )
    source = FrameworkSource(document_id="d1", document_name="Doc", origin="drive_search")
    docs_client = _FakeDocsClient({"d1": tabs})
    outcome = run_framework(
        source, docs_client=docs_client, strategy=_strategy(), profile=Profile(),
        throttle=HostThrottle(), stats=FetchStats(), check_site=True, check_published=True,
    )
    assert outcome.link_audit is not None
    assert outcome.link_audit.in_copy.state == "mismatch"
    assert outcome.link_audit.in_copy.detail == "0/1 framework link(s) in copy"


def test_link_audit_match_when_framework_link_is_in_copy_and_on_site(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    html = b'<html><body><h1>H</h1><p><a href="/menu/">Menu</a></p></body></html>'
    monkeypatch.setattr(site_module, "fetch", _fake_fetch_factory(html))
    tabs = (
        _tab("Framework", [_table(
            (["URL"], ["https://example.com/a"]),
            (["Internal Link + Anchor Text"], ["our menu (/menu/)"]),
        )]),
        _tab("Content", [_linked_para(_linked_run("our menu", "https://example.com/menu/"))]),
    )
    source = FrameworkSource(document_id="d1", document_name="Doc", origin="drive_search")
    docs_client = _FakeDocsClient({"d1": tabs})
    outcome = run_framework(
        source, docs_client=docs_client, strategy=_strategy(), profile=Profile(),
        throttle=HostThrottle(), stats=FetchStats(), check_site=True, check_published=True,
    )
    assert outcome.link_audit is not None
    assert outcome.link_audit.in_copy.state == "match"
    assert outcome.link_audit.on_site.state == "match"


def test_link_audit_picks_the_primary_language_tab_not_the_first_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """task-95 §2.3, reusing task-92 §2.5's coverage-based role: the
    Swedish tab matches the live (Swedish) page, so its links — not the
    English tab's — are what gets audited, even though English comes
    first in the document."""
    html = (
        b'<html lang="sv-SE"><body><h1>Spirit Box</h1>'
        b'<p><a href="/prs/">PRS</a> Svensk text om produkten.</p></body></html>'
    )
    monkeypatch.setattr(site_module, "fetch", _fake_fetch_factory(html))
    tabs = (
        _tab("Framework", [_table((["URL"], ["https://laxton.se/p"]))]),
        _tab("English Content", [
            _linked_para(_linked_run("English link", "https://laxton.se/en-only/")),
        ]),
        _tab("Swedish Content", [
            _linked_para(_linked_run("PRS", "https://laxton.se/prs/")),
            _para("Svensk text om produkten."),
        ]),
    )
    source = FrameworkSource(document_id="d1", document_name="Doc", origin="drive_search")
    docs_client = _FakeDocsClient({"d1": tabs})
    outcome = run_framework(
        source, docs_client=docs_client, strategy=_strategy(), profile=Profile(),
        throttle=HostThrottle(), stats=FetchStats(), check_site=True, check_published=True,
    )
    assert outcome.link_audit is not None
    targets = {row.target for row in outcome.link_audit.rows}
    assert any("prs" in t for t in targets)
    assert not any("en-only" in t for t in targets)


def test_link_audit_runs_even_when_check_published_is_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The link chain (task-95) is independent of task-92's sentence-diff
    lane — it must still compute when `check_published=False` disables
    `copy_diffs`, falling back to the first copy tab."""
    html = b'<html><body><h1>H</h1><p><a href="/menu/">Menu</a></p></body></html>'
    monkeypatch.setattr(site_module, "fetch", _fake_fetch_factory(html))
    tabs = (
        _tab("Framework", [_table(
            (["URL"], ["https://example.com/a"]),
            (["Internal Link + Anchor Text"], ["our menu (/menu/)"]),
        )]),
        _tab("Content", [_linked_para(_linked_run("our menu", "https://example.com/menu/"))]),
    )
    source = FrameworkSource(document_id="d1", document_name="Doc", origin="drive_search")
    docs_client = _FakeDocsClient({"d1": tabs})
    outcome = run_framework(
        source, docs_client=docs_client, strategy=_strategy(), profile=Profile(),
        throttle=HostThrottle(), stats=FetchStats(), check_site=True, check_published=False,
    )
    assert outcome.copy_diffs == ()
    assert outcome.link_audit is not None
    assert outcome.link_audit.in_copy.state == "match"
