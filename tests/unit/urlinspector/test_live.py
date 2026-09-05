"""`app.urlinspector.live.run_live_check` — the two real network calls are
patched at the point of use (same technique `tests/unit/monitor/test_blocked_handling.py`
uses for `app.monitor.runner.fetch`), so this exercises the real
`extract_signals`/`is_indexable`/QA logic without touching the network.
"""

from __future__ import annotations

from unittest.mock import patch

from app.monitor.fetcher import FetchResult
from app.tools.redirects.trace import Hop, TraceResult, Verdict
from app.urlinspector.live import run_live_check

URL = "https://acme.com/a"


def _trace_ok(final_url=URL, final_status=200):
    return TraceResult(
        input_url=URL, normalized_url=URL, verdict=Verdict.ok_direct,
        hops=(Hop(index=0, url=URL, status=200, status_text="OK"),),
        final_url=final_url, final_status=final_status,
    )


def _trace_blocked():
    return TraceResult(
        input_url=URL, normalized_url=URL, verdict=Verdict.blocked,
        hops=(Hop(index=0, url=URL, status=429, status_text="Too Many Requests"),),
        final_url=URL, final_status=429, error_detail="rate limited",
    )


def _fetch_ok(body: bytes, *, content_type="text/html; charset=utf-8"):
    return FetchResult(
        requested_url=URL, final_url=URL, status=200, hop_count=0,
        headers={"Content-Type": content_type}, body=body, truncated_body=False,
        elapsed_ms=5, error=None,
    )


def _fetch_blocked():
    return FetchResult(
        requested_url=URL, final_url=URL, status=429, hop_count=0,
        headers={}, body=b"blocked", truncated_body=False,
        elapsed_ms=5, error="blocked",
    )


HTML = (
    b"<html><head><title>Acme | Home</title>"
    b'<meta name="description" content="A real, sufficiently long description '
    b"that should clear the minimum length check comfortably for this fixture.\">"
    b'<link rel="canonical" href="https://acme.com/a">'
    b"</head><body><h1>Welcome</h1><p>Some visible text on the page.</p></body></html>"
)


class TestBlockedShortCircuitsBeforeSecondFetch:
    def test_trace_blocked_never_calls_fetch(self, session):
        with (
            patch("app.urlinspector.live.trace_redirects", return_value=_trace_blocked()),
            patch("app.urlinspector.live.fetch", side_effect=AssertionError("must not be called")),
        ):
            dto = run_live_check(session, client_id=None, url=URL, with_schema=False)

        assert dto.verdict == "blocked"
        assert dto.signals is None
        assert dto.http_status == 429


class TestSuccessfulLiveCheck:
    def test_full_signals_and_indexable(self, session):
        with (
            patch("app.urlinspector.live.trace_redirects", return_value=_trace_ok()),
            patch("app.urlinspector.live.fetch", return_value=_fetch_ok(HTML)),
        ):
            dto = run_live_check(session, client_id=None, url=URL, with_schema=False)

        assert dto.verdict == "ok"
        assert dto.indexable is True
        assert dto.signals is not None
        assert dto.signals.title == "Acme | Home"
        assert dto.signals.canonical == "https://acme.com/a"
        assert dto.signals.h1 == "Welcome"
        assert dto.redirect_chain[0].status == 200

    def test_noindex_page_is_not_indexable(self, session):
        html = HTML.replace(b"<head>", b'<head><meta name="robots" content="noindex">')
        with (
            patch("app.urlinspector.live.trace_redirects", return_value=_trace_ok()),
            patch("app.urlinspector.live.fetch", return_value=_fetch_ok(html)),
        ):
            dto = run_live_check(session, client_id=None, url=URL, with_schema=False)

        assert dto.indexable is False

    def test_findings_flow_through_the_same_qa_engine(self, session):
        # Missing description entirely -> `meta-desc-missing` from the exact
        # same rules engine task-57's Page Inventory uses (task-61 §2.4).
        html = HTML.replace(
            b'<meta name="description" content="A real, sufficiently long description '
            b'that should clear the minimum length check comfortably for this fixture.">',
            b"",
        )
        with (
            patch("app.urlinspector.live.trace_redirects", return_value=_trace_ok()),
            patch("app.urlinspector.live.fetch", return_value=_fetch_ok(html)),
        ):
            dto = run_live_check(session, client_id=None, url=URL, with_schema=False)

        checks = {f.check for f in dto.findings}
        assert "meta-desc-missing" in checks


class TestFetchItselfBlocked:
    def test_fetch_blocked_after_trace_passed(self, session):
        # Trace can pass (a redirect-only probe) while the real page fetch
        # itself still hits the antibot wall — both are honest, independent
        # outcomes, never conflated (task-61 §2.4).
        with (
            patch("app.urlinspector.live.trace_redirects", return_value=_trace_ok()),
            patch("app.urlinspector.live.fetch", return_value=_fetch_blocked()),
        ):
            dto = run_live_check(session, client_id=None, url=URL, with_schema=False)

        assert dto.verdict == "blocked"
        assert dto.signals is None


class TestSchemaOptIn:
    def test_with_schema_false_never_touches_schema_repo(self, session):
        with (
            patch("app.urlinspector.live.trace_redirects", return_value=_trace_ok()),
            patch("app.urlinspector.live.fetch", return_value=_fetch_ok(HTML)),
            patch(
                "app.urlinspector.live._persist_schema_check",
                side_effect=AssertionError("must not run when with_schema=False"),
            ),
        ):
            dto = run_live_check(session, client_id=None, url=URL, with_schema=False)

        assert dto.schema_check_id is None

    def test_schema_check_failure_does_not_sink_the_live_panel(self, session):
        with (
            patch("app.urlinspector.live.trace_redirects", return_value=_trace_ok()),
            patch("app.urlinspector.live.fetch", return_value=_fetch_ok(HTML)),
            patch(
                "app.urlinspector.live._persist_schema_check",
                side_effect=RuntimeError("third-party fetch failed"),
            ),
        ):
            dto = run_live_check(session, client_id=None, url=URL, with_schema=True)

        assert dto.verdict == "ok"
        assert dto.schema_check_id is None
