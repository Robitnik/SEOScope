"""Оркестрація одного прогону (task-56 §5): fetch → extract → classify →
validate → generate. Мережа — той самий SSRF-гард/UA/no-cache, що в Alt
Text/Redirect Checker (`app.tools.http_safety`); fetch-логіка дзеркалить
`alt_text.verify.fetch_page` (той самий контракт: один синхронний GET без
кешу), а не повнофункціональний `app.monitor.fetcher.fetch` — той належить
монітору (фінгерпринт-діф, ретраї, лейни) і є чужою зоною.
"""

from __future__ import annotations

import urllib.error
import urllib.request
from dataclasses import dataclass, field

from app.tools.http_safety import (
    NO_CACHE_HEADERS,
    USER_AGENT,
    GuardedRedirectHandler,
    UnsafeUrlError,
    assert_public_http_url,
)
from app.tools.schema_markup.classify import ClassifyResult, classify_page
from app.tools.schema_markup.extract import ExtractionResult, extract_all
from app.tools.schema_markup.generate import Fact, GenerationResult, PageFacts, generate_for_kind
from app.tools.schema_markup.signals import extract_signals
from app.tools.schema_markup.validate import Finding, Severity, validate_all
from app.tools.schema_markup.vocab import load_vocabulary

REQUEST_TIMEOUT_SECONDS = 15.0
MAX_BODY_BYTES = 5 * 1024 * 1024

Verdict = str  # "clean" | "has_gaps" | "missing" | "fetch_failed"


@dataclass(frozen=True)
class FetchOutcome:
    ok: bool
    http_status: int | None
    final_url: str | None
    html: str | None
    error: str | None


def fetch_page(url: str) -> FetchOutcome:
    """Кидає нічого — усі збої (SSRF-гард, DNS, timeout, non-2xx) повертаються
    як `FetchOutcome(ok=False, ...)`, щоб виклик з API ніколи не перетворював
    "сайт клієнта недоступний" на 500 (PLAN §12: чесний стан, не крах).
    """
    try:
        assert_public_http_url(url)
    except UnsafeUrlError as exc:
        return FetchOutcome(ok=False, http_status=None, final_url=None, html=None, error=str(exc))
    request = urllib.request.Request(  # noqa: S310 - схема перевірена гардом вище
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en-US,en;q=0.9",
            **NO_CACHE_HEADERS,
        },
    )
    opener = urllib.request.build_opener(GuardedRedirectHandler)
    try:
        with opener.open(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            raw = response.read(MAX_BODY_BYTES + 1)
            charset = response.headers.get_content_charset() or "utf-8"
            return FetchOutcome(
                ok=True,
                http_status=response.status,
                final_url=response.geturl(),
                html=raw[:MAX_BODY_BYTES].decode(charset, errors="replace"),
                error=None,
            )
    except urllib.error.HTTPError as exc:
        return FetchOutcome(
            ok=False, http_status=exc.code, final_url=None, html=None, error=f"HTTP {exc.code}"
        )
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return FetchOutcome(ok=False, http_status=None, final_url=None, html=None, error=str(exc))


@dataclass
class CheckResult:
    url: str
    final_url: str | None
    http_status: int | None
    verdict: Verdict
    page_kind: ClassifyResult | None
    found_types: tuple[str, ...]
    findings: list[Finding] = field(default_factory=list)
    generated: GenerationResult | None = None
    vocab_version: str = "unknown"
    error: str | None = None
    extraction: ExtractionResult | None = None


def _derive_verdict(extraction: ExtractionResult, findings: list[Finding]) -> Verdict:
    if not extraction.has_valid_markup and not extraction.has_broken_markup:
        return "missing"
    if extraction.has_broken_markup or any(f.severity == Severity.error for f in findings):
        return "has_gaps"
    return "clean" if not findings else "has_gaps"


def run_check(
    url: str, *, organization_name: str | None = None, fetcher: object | None = None
) -> CheckResult:
    """`fetcher` — an injectable `Callable[[str], FetchOutcome]` for tests
    (mirrors the `Fetcher` seam already used in `alt_text.verify`); defaults
    to the real network `fetch_page`.
    """
    vocab = load_vocabulary()
    fetch = fetcher if fetcher is not None else fetch_page
    outcome: FetchOutcome = fetch(url)  # type: ignore[operator]

    if not outcome.ok or outcome.html is None:
        return CheckResult(
            url=url,
            final_url=outcome.final_url,
            http_status=outcome.http_status,
            verdict="fetch_failed",
            page_kind=None,
            found_types=(),
            findings=[],
            generated=None,
            vocab_version=vocab.version,
            error=outcome.error,
        )

    effective_url = outcome.final_url or url
    extraction = extract_all(outcome.html)
    signals = extract_signals(outcome.html)
    found_types = tuple(sorted({t for e in extraction.all_entities for t in e.types}))

    kind = classify_page(effective_url, existing_types=found_types)
    findings = validate_all(extraction, vocab)

    org_name_fact = (
        Fact(organization_name, "client:name")
        if organization_name
        else (Fact(signals.og_site_name, "page:og:site_name") if signals.og_site_name else None)
    )
    facts = PageFacts(
        url=effective_url,
        title=Fact(signals.title, "page:title") if signals.title else None,
        h1=Fact(signals.h1, "page:h1") if signals.h1 else None,
        og_image=Fact(signals.og_image, "page:og:image") if signals.og_image else None,
        published_time=(
            Fact(signals.article_published_time, "page:article:published_time")
            if signals.article_published_time
            else None
        ),
        modified_time=(
            Fact(signals.article_modified_time, "page:article:modified_time")
            if signals.article_modified_time
            else None
        ),
        organization_name=org_name_fact,
    )
    generated = generate_for_kind(kind.kind, facts, existing_types=frozenset(found_types))
    verdict = _derive_verdict(extraction, findings)

    return CheckResult(
        url=url,
        final_url=outcome.final_url,
        http_status=outcome.http_status,
        verdict=verdict,
        page_kind=kind,
        found_types=found_types,
        findings=findings,
        generated=generated,
        vocab_version=vocab.version,
        error=None,
        extraction=extraction,
    )
