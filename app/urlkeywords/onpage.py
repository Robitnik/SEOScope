"""On-page term extraction for the URL Inspector's keywords panel
(task-64 §3.2) — a frequency count over the page's own extracted content
tokens, not a targeted keyword check like `seo_audit.policy`'s other
keyword functions (those all take a keyword to test FOR; this discovers
candidates instead, which is why it lives here rather than in
`policy_shim` alongside straight pass-throughs).
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from app.urlkeywords.policy_shim import topic_tokens

_TOP_N = 24
_MIN_PHRASE_COUNT = 2


@dataclass(frozen=True)
class OnPageTerm:
    term: str
    count: int
    is_phrase: bool
    in_title: bool
    in_h1: bool
    in_description: bool


def _present(term_tokens: frozenset[str], haystack: frozenset[str]) -> bool:
    return bool(term_tokens) and term_tokens.issubset(haystack)


def extract_on_page_terms(
    visible_text: str,
    *,
    title: str | None,
    h1: str | None,
    description: str | None,
    top_n: int = _TOP_N,
) -> list[OnPageTerm]:
    """Top single terms + repeated adjacent two-term phrases from the page's
    visible text.

    Phrases are formed from adjacency WITHIN the already stopword-filtered
    token stream — a documented simplification, not an oversight: "best
    hearing aids in nyc" tokenizes to [hear, aid, nyc], and the only
    candidate phrase is "hear aid", never "aid in" or "in nyc" — function
    words can never survive into a phrase. This over- and under-generates
    relative to a true positional n-gram model, in exchange for reusing the
    one tokenizer/stopword list the rest of the app already trusts
    (`seo_audit.policy.topic_tokens`, via `policy_shim`) instead of a second,
    bespoke one.
    """
    tokens = topic_tokens(visible_text or "")
    if not tokens:
        return []

    title_tokens = frozenset(topic_tokens(title or ""))
    h1_tokens = frozenset(topic_tokens(h1 or ""))
    desc_tokens = frozenset(topic_tokens(description or ""))

    unigram_counts = Counter(tokens)
    bigram_counts = Counter(
        f"{a} {b}" for a, b in zip(tokens, tokens[1:], strict=False) if a != b
    )

    terms: list[OnPageTerm] = []
    for term, count in unigram_counts.most_common(top_n):
        term_tokens = frozenset({term})
        terms.append(
            OnPageTerm(
                term=term,
                count=count,
                is_phrase=False,
                in_title=_present(term_tokens, title_tokens),
                in_h1=_present(term_tokens, h1_tokens),
                in_description=_present(term_tokens, desc_tokens),
            )
        )
    for phrase, count in bigram_counts.most_common(top_n):
        if count < _MIN_PHRASE_COUNT:
            continue
        phrase_tokens = frozenset(phrase.split(" "))
        terms.append(
            OnPageTerm(
                term=phrase,
                count=count,
                is_phrase=True,
                in_title=_present(phrase_tokens, title_tokens),
                in_h1=_present(phrase_tokens, h1_tokens),
                in_description=_present(phrase_tokens, desc_tokens),
            )
        )
    terms.sort(key=lambda t: (-t.count, t.term))
    return terms[:top_n]
