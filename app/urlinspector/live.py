"""On-demand micro technical audit for the URL Inspector (task-61 §2.4).

Two SSRF-guarded, blocking (threadpool-wrapped by the API layer) calls —
never persisted (task-61 plan §2.5: a 7th table of page-meta-signals next
to `monitor_pages`/`page_inventory_items`/`archive_snapshots`/
`schema_markup_checks` would not make any of them more true; fixing a state
in history is the existing "Check with monitor" button's job):

1. `app.tools.redirects.trace.trace_redirects` — the real hop-by-hop
   redirect chain + an honest verdict (including antibot `blocked`),
   reused rather than reimplemented (task-29 already solved this exactly).
2. `app.monitor.fetcher.fetch` + `app.monitor.page.extract_signals` — the
   full page signals (title/description/h1/canonical/robots/hreflang/
   JSON-LD types/word count/analytics ids), same functions the Site
   Monitor itself runs on every crawl.

`with_schema=True` additionally runs (and, unlike the rest of this module,
DOES persist via the existing `schema_markup_checks` table/repository —
task-56 already owns that record) `app.tools.schema_markup.service.run_check`
— a THIRD independent fetch, only on explicit request.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.db.repositories.clients import ClientRepository
from app.db.repositories.schema_markup import SchemaMarkupCheckInput, SchemaMarkupRepository
from app.monitor.fetcher import FetchStats, fetch
from app.monitor.page import decode_body, extract_signals, is_indexable
from app.schemas.url_inspector import (
    UrlInspectorLiveDTO,
    UrlLiveHopDTO,
    UrlLiveSignalsDTO,
)
from app.tools.http_safety import HostThrottle
from app.tools.redirects.trace import TraceOptions, Verdict, trace_redirects
from app.tools.schema_markup.service import CheckResult, run_check
from app.urlinspector.metaqa import qa_findings

_BLOCKED_VERDICTS = frozenset({Verdict.blocked, Verdict.unsafe_url})


def _schema_findings_json(result: CheckResult) -> list[dict[str, object]]:
    return [
        {
            "level": f.level.value,
            "severity": f.severity.value,
            "code": f.code,
            "message": f.message,
            "entity_types": list(f.entity_types),
            "property_name": f.property_name,
        }
        for f in result.findings
    ]


def _persist_schema_check(session: Session, *, client_id: str | None, url: str) -> str | None:
    """Best-effort: a failure here degrades the live panel to "no schema_check_id",
    never fails the whole live check (task-61 §2.4 isolation, same principle
    PLAN §9.1 applies to a collection cell)."""
    client_name = None
    if client_id:
        client = ClientRepository(session).get(client_id)
        client_name = client.name if client else None

    result = run_check(url, organization_name=client_name)
    check = SchemaMarkupRepository(session).create_check(
        SchemaMarkupCheckInput(
            client_id=client_id,
            url=result.url,
            final_url=result.final_url,
            http_status=result.http_status,
            verdict=result.verdict,
            page_kind=result.page_kind.kind if result.page_kind else None,
            found_types=result.found_types,
            findings=_schema_findings_json(result),
            # task-61 follow-up: full generated-schema shaping left to /tools/schema-markup.
            generated=None,
            raw_markup=[],
            vocab_version=result.vocab_version,
            error=result.error,
        )
    )
    session.commit()
    return check.id


def run_live_check(
    session: Session, *, client_id: str | None, url: str, with_schema: bool
) -> UrlInspectorLiveDTO:
    trace = trace_redirects(url, options=TraceOptions())

    if trace.verdict in _BLOCKED_VERDICTS:
        return UrlInspectorLiveDTO(
            input_url=url,
            final_url=trace.final_url,
            http_status=trace.final_status,
            redirect_chain=[
                UrlLiveHopDTO(index=h.index, url=h.url, status=h.status, location=h.location)
                for h in trace.hops
            ],
            verdict=trace.verdict.value,
            error=trace.error_detail,
            generated_at=datetime.now(UTC),
        )

    stats = FetchStats()
    throttle = HostThrottle()
    fetched = fetch(url, throttle=throttle, stats=stats)

    hops = [
        UrlLiveHopDTO(index=h.index, url=h.url, status=h.status, location=h.location)
        for h in trace.hops
    ]

    if not fetched.ok or fetched.body is None:
        verdict = "blocked" if fetched.error == "blocked" else (fetched.error or "fetch_failed")
        return UrlInspectorLiveDTO(
            input_url=url,
            final_url=fetched.final_url,
            http_status=fetched.status,
            redirect_chain=hops,
            verdict=verdict,
            error=fetched.error_detail,
            generated_at=datetime.now(UTC),
        )

    html = decode_body(fetched.body, content_type=fetched.headers.get("Content-Type"))
    signals = extract_signals(html)
    indexable = is_indexable(
        meta_robots=signals.meta_robots,
        x_robots_tag=fetched.headers.get("X-Robots-Tag"),
        status=fetched.status,
    )

    schema_check_id = None
    if with_schema:
        try:
            schema_check_id = _persist_schema_check(session, client_id=client_id, url=url)
        except Exception:  # noqa: BLE001 — a live 3rd-party fetch failure must not sink the whole panel
            session.rollback()
            schema_check_id = None

    return UrlInspectorLiveDTO(
        input_url=url,
        final_url=fetched.final_url,
        http_status=fetched.status,
        redirect_chain=hops,
        verdict="ok",
        indexable=indexable,
        signals=UrlLiveSignalsDTO(
            title=signals.title,
            description=signals.description,
            h1=signals.h1,
            canonical=signals.canonical,
            meta_robots=signals.meta_robots,
            x_robots_tag=fetched.headers.get("X-Robots-Tag"),
            hreflang=[[lang, href] for lang, href in signals.hreflang],
            json_ld_types=signals.json_ld_types,
            word_count=signals.word_count,
            analytics_ids=signals.analytics_ids,
        ),
        findings=qa_findings(
            url,
            title=signals.title,
            description=signals.description,
            h1=signals.h1,
            status=fetched.status,
        ),
        schema_check_id=schema_check_id,
        generated_at=datetime.now(UTC),
    )
