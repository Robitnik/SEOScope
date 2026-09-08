"""Перенесені правила, яких немає в `seo_audit.policy` (той шар — policy_bridge.py).

Джерела (усі прочитані повністю під час дослідження task-53):
  - AISeoTools docs/PageFramework/framework-rules.md — meta MIN, ієрархія заголовків
  - AISeoTools docs/BlogFramework/blog-rules.md — блогові банди
    (keywords/H2/H3/FAQ/LSI/links)
  - AISeoTools docs/brand-voice.md — hard rules, word-swap таблиця (14 рядків)
  - Wikipedia:Signs of AI writing — таксономія слопу поза AI_SLOP_PHRASES
  - GOREPORT_..._SCHEDULE_OF_CONDITION_..._08182026.docx — секції "SEO Rules" /
    "SEO Pre-publish checklist"

Severity: error = документ не можна віддавати назовні; warn = рішення людини;
info = nudge з пропонованою заміною. Жодне правило тут не стверджує «це написав
AI» — лише «ця фраза замінна» (обмеження, яке явно ставить собі й Vale-пакет
deslop: корпусні знахідки, не подокументні).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.contentqa.findings import Finding, excerpt
from app.contentqa.lexicon import Lexicon, get_lexicon
from app.contentqa.model import ContentDoc, ContentItem
from app.contentqa.policy_bridge import text_similarity

# ── профіль ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Profile:
    kind: str = "page"  # "page" | "blog"
    intent: str = "commercial"  # "commercial" | "informational" | "navigational"
    locale: str = "en-US"  # "en-US" | "en-GB" | "sv-SE"
    brand_names: tuple[str, ...] = ()
    # False = дублі/канібалізація (check_cross_page/check_meta_duplicates)
    # рахуються в межах аркуша, не по всьому документу (task-87 §1.3) —
    # дефолт True зберігає поводження звітів, збережених до task-87.
    cross_sheet: bool = True

    @property
    def language(self) -> str:
        """ISO 639-1 з `locale` — "en" для en-US І en-GB (обидва йдуть через
        policy_bridge/AISeoTools незмінно), "sv" для sv-SE. Лейни питають цю
        властивість, а не `locale` напряму (task-87 §1.1): додати мову —
        значить дописати `app/contentqa/lexicon/<code>.py` і зареєструвати
        його в `lexicon.get_lexicon()`, не чіпаючи жодного правила тут."""
        return (self.locale or "en-US").split("-")[0].lower()

    @property
    def lexicon(self) -> Lexicon | None:
        """`None` для "en" (лейни йдуть через policy_bridge як і раніше) і
        для будь-якої мови без написаного лексикону."""
        return get_lexicon(self.language)


# ── meta MIN (MAX живе в policy_bridge — це framework-qa.py власний додаток) ──

TITLE_MIN = 50
DESC_MIN = 140

# ── блогові банди (blog-rules.md) ─────────────────────────────────────────────

BLOG_KEYWORDS_MIN, BLOG_KEYWORDS_MAX = 5, 10
BLOG_H2_MIN, BLOG_H3_MIN = 4, 6
FAQ_MIN, FAQ_MAX, FAQ_MAX_WORDS = 4, 12, 10
LSI_MIN, LSI_MAX = 20, 50
COMPETITORS_MIN = 3

# ── лінки за типом сторінки (DOCX §Links, blog-rules.md) ──────────────────────
# {intent: (min, max)}; short (<600 слів) сторінка — окремий мінімум 1.
LINKS_BY_INTENT: dict[str, tuple[int, int]] = {
    "commercial": (2, 4),
    "navigational": (2, 4),
    "informational": (3, 6),
}
LINKS_SHORT_PAGE_MIN = 1
LINKS_SHORT_PAGE_WORD_CEILING = 600
EXTERNAL_LINKS_MAX = 2
IMAGES_MIN = 2
IMAGES_MAX = 4

# ── читабельність (readability.py споживає це) ────────────────────────────────

HEMINGWAY_GRADE_MIN, HEMINGWAY_GRADE_MAX = 7.0, 10.0
ANSWER_FIRST_MIN_WORDS, ANSWER_FIRST_MAX_WORDS = 40, 80
SECTION_MAX_WORDS = 300
SENTENCE_MAX_WORDS = 30

# ── brand-voice: word swaps (docs/brand-voice.md §Word swaps, дослівно) ───────

WORD_SWAPS: dict[str, str] = {
    "utilize": "use",
    "leverage": "use",
    "in order to": "to",
    "at this point in time": "now",
    "revert": "reply, get back to you",
    "kindly be advised": "(just say the thing)",
    "please find attached": "I have attached, attached is",
    "reach out": "contact, email, message",
    "circle back": "follow up, check in",
    "touch base": "follow up, check in",
    "synergy": "(delete; name the actual benefit)",
    "synergize": "(delete; name the actual benefit)",
    "cutting-edge": "(delete; show the result instead)",
    "best-in-class": "(delete; show the result instead)",
    "guaranteed results": "(never promise rankings)",
    "#1 rankings": "(never promise rankings)",
    "very unique": "unique",
    "more optimal": "better",
    "in the ever-evolving landscape of": "(delete the whole phrase)",
}

# ── brand-voice: термінологічна дисципліна (§5) ───────────────────────────────

TERMINOLOGY_BANNED: dict[str, str] = {
    "gmb": "GBP (Google Business Profile)",
    "gmp": "GBP (Google Business Profile)",
    "suggested fix": "Recommended Action",
    "recommended fix": "Recommended Action",
    "suggested action": "Recommended Action",
}

# ── brand-voice: US/GB локаль ──────────────────────────────────────────────────
# (§2 + word-swap "organisation, optimise, analyse")
# {en-US canonical: en-GB variant}; напрямок перевірки залежить від profile.locale.

US_GB_SPELLING: dict[str, str] = {
    "color": "colour",
    "optimize": "optimise",
    "analyze": "analyse",
    "behavior": "behaviour",
    "center": "centre",
    "organization": "organisation",
    "standardize": "standardise",
    "personalize": "personalise",
    "customize": "customise",
    "recognize": "recognise",
    "realize": "realise",
    "specialize": "specialise",
    "program": "programme",
    "license": "licence",
    "defense": "defence",
    "favorite": "favourite",
    "traveling": "travelling",
    "modeling": "modelling",
    "catalog": "catalogue",
}

CONTRACTIONS_RE = re.compile(
    r"\b(don't|won't|can't|isn't|aren't|wasn't|weren't|it's|we'll|we're|"
    r"you're|they're|didn't|doesn't|haven't|hasn't|couldn't|wouldn't|shouldn't)\b",
    re.IGNORECASE,
)

MONEY_TRIGGER_RE = re.compile(
    r"\b(fee|salary|retainer|hourly rate|commission|wage|payroll)\b", re.IGNORECASE
)
CURRENCY_RE = re.compile(r"[$£€]\s?\d|\b\d+(\.\d+)?\s?(usd|gbp|eur)\b", re.IGNORECASE)

# ── слоп поза AI_SLOP_PHRASES (Wikipedia: Signs of AI writing) ────────────────

NEGATIVE_PARALLELISM_RE = re.compile(
    r"\bnot only\b.{0,40}\bbut also\b|\bnot just\b.{0,40}\bbut\b", re.IGNORECASE
)
COPULA_AVOIDANCE_PHRASES = ("serves as", "stands as", "functions as", "boasts a", "boasts an")
VAGUE_ATTRIBUTION_PHRASES = (
    "observers have noted",
    "industry reports suggest",
    "some experts argue",
    "studies show that",
    "reports indicate that",
)
PARTICIPLE_CHAIN_RE = re.compile(
    r",\s+(highlighting|emphasizing|underscoring|showcasing|reflecting)\s+(the|its|their)\b",
    re.IGNORECASE,
)
UNDUE_SIGNIFICANCE_PHRASES = (
    "pivotal moment",
    "significant shift",
    "key turning point",
    "marks a turning point",
    "represents a significant",
)
COLLABORATIVE_VOICE_PHRASES = (
    "let's explore",
    "let us explore",
    "as we discussed",
    "you might notice",
    "we've covered",
    "as you can see",
)
RULE_OF_THREE_RE = re.compile(r"\b\w+,\s\w+,?\sand\s\w+\b")
RULE_OF_THREE_DOC_THRESHOLD = 3

LLM_ARTIFACT_PATTERNS = (
    "contentreference",
    "oaicite",
    "turn0search",
    "turn0news",
    "grok_render_citation_card_json",
    "grok_card",
    "ppl-ai-file-upload",
    "attached_file",
)
LLM_ARTIFACT_CITE_RE = re.compile(r"\[cite:\s*\d+\]|\[span_\d+\]", re.IGNORECASE)

PLACEHOLDER_PATTERNS = (
    "tbd",
    "[citation needed]",
    "lorem ipsum",
    "to be updated",
    "todo",
    "fixme",
    "[insert",
    "placeholder",
)

EMOJI_RE = re.compile("[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF]")

_WORD_RE = re.compile(r"[^\W\d_]+", re.UNICODE)


def _words(text: str) -> list[str]:
    return [w.lower() for w in _WORD_RE.findall(text or "")]


def _plural(n: int, noun: str) -> str:
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


def _phrase_hits(text: str, phrases: tuple[str, ...] | tuple[()]) -> list[str]:
    low = (text or "").lower()
    return [p for p in phrases if p in low]


def _fields(item: ContentItem) -> list[tuple[str, str]]:
    """(назва поля, текст) для всього, що ми пишемо самі — не для body, який
    у DOCX містить прозу секцій і перевіряється окремо в readability.py."""
    out: list[tuple[str, str]] = []
    for name, value in (
        ("title", item.title),
        ("description", item.description),
        ("h1", item.h1),
    ):
        if value:
            out.append((name, value))
    for level, text in item.headings:
        out.append((level.lower(), text))
    return out


# ── B: STRUCTURE ───────────────────────────────────────────────────────────


def check_structure(item: ContentItem, profile: Profile) -> list[Finding]:
    f: list[Finding] = []
    headings = item.headings

    h1_count = sum(1 for lvl, _ in headings if lvl.upper() == "H1")
    if item.h1 == "":
        f.append(Finding("h1-missing", "error", item.ref, "H1 field is present and empty."))
    if h1_count > 1:
        h1_texts = "; ".join(text for lvl, text in headings if lvl.upper() == "H1")
        f.append(Finding(
            "h1-multiple", "error", item.ref,
            f"{h1_count} H1 headings found; exactly one is required.",
            value=excerpt(h1_texts),
        ))
    if item.h1 and item.title and item.h1.strip().lower() == item.title.strip().lower():
        f.append(Finding(
            "h1-equals-title", "info", item.ref,
            "H1 is identical to the meta title; they may differ.",
            value=excerpt(item.h1),
        ))

    seen_h2 = False
    seen_levels: list[str] = []
    for level, text in headings:
        lvl = level.upper()
        if lvl == "H2":
            seen_h2 = True
        elif lvl == "H3" and not seen_h2:
            f.append(Finding(
                "orphan-h3", "error", item.ref, f"H3 {text!r} has no preceding H2.",
                value=excerpt(text),
            ))
        seen_levels.append(lvl)
        if lvl == "H4" and "H3" not in seen_levels[-3:]:
            f.append(Finding(
                "heading-hierarchy-skip", "error", item.ref,
                f"H4 {text!r} appears without an H3 parent.",
                value=excerpt(text),
            ))

    for level in ("H2", "H3"):
        texts = [t for lvl, t in headings if lvl.upper() == level]
        for i in range(len(texts)):
            for j in range(i + 1, len(texts)):
                if text_similarity(texts[i], texts[j]) >= 0.8:
                    f.append(Finding(
                        "duplicate-heading", "warn", item.ref,
                        f"{level} headings look duplicated: {texts[i]!r} / {texts[j]!r}",
                        value=excerpt(f"{texts[i]} / {texts[j]}"),
                    ))

    if item.body:
        first_para = item.body.strip().split("\n\n", 1)[0]
        n = len(_words(first_para))
        if not (ANSWER_FIRST_MIN_WORDS <= n <= ANSWER_FIRST_MAX_WORDS):
            f.append(Finding(
                "answer-first-missing", "warn", item.ref,
                f"Opening paragraph is {_plural(n, 'word')}; expected "
                f"{ANSWER_FIRST_MIN_WORDS}-{ANSWER_FIRST_MAX_WORDS}.",
                value=excerpt(first_para),
            ))
        for section in _split_sections(item.body):
            wc = len(_words(section))
            if wc > SECTION_MAX_WORDS:
                f.append(Finding(
                    "section-over-300-words", "warn", item.ref,
                    f"Section is {wc} words with no subheading/list/table break "
                    f"(limit {SECTION_MAX_WORDS}).",
                    value=excerpt(section),
                ))

    faq_qs = _faq_questions(headings)
    if faq_qs:
        # FAQ_MIN/MAX/MAX_WORDS — blog-rules.md, лише для блогів. Для сторінок
        # framework-rules.md прямо каже: "No minimum or maximum; include only
        # useful, answerable questions" — застосування блогового банду до
        # page-фреймворку дало б хибне спрацювання (перевірено на живому
        # GOREPORT SOC-фреймворку: 3 FAQ, коректно для сторінки, хибний warn
        # при бенді 4-12).
        if profile.kind == "blog":
            if not (FAQ_MIN <= len(faq_qs) <= FAQ_MAX):
                f.append(Finding(
                    "faq-count", "warn", item.ref,
                    f"{_plural(len(faq_qs), 'FAQ question')}; band is {FAQ_MIN}-{FAQ_MAX}.",
                ))
        for q in faq_qs:
            if "?" not in q:
                f.append(Finding(
                    "faq-not-question", "warn", item.ref,
                    f"FAQ heading is not phrased as a question: {q!r}",
                    value=excerpt(q),
                ))
            if profile.kind == "blog" and len(_words(q)) > FAQ_MAX_WORDS:
                f.append(Finding(
                    "faq-too-long", "warn", item.ref,
                    f"FAQ question over {FAQ_MAX_WORDS} words: {q!r}",
                    value=excerpt(q),
                ))

    return f


def _split_sections(body: str) -> list[str]:
    parts = re.split(r"\n(?=H[123]:\s)", body)
    return [p for p in parts if p.strip()]


def _faq_questions(headings: tuple[tuple[str, str], ...]) -> list[str]:
    out: list[str] = []
    in_faq = False
    for level, text in headings:
        if level.upper() == "H2":
            in_faq = bool(re.search(r"\bFAQ\b|frequently asked", text, re.IGNORECASE))
            continue
        if in_faq and level.upper() == "H3":
            out.append(text)
    return out


# ── C: LINKS & MEDIA ─────────────────────────────────────────────────────────


def check_links(item: ContentItem, profile: Profile) -> list[Finding]:
    f: list[Finding] = []
    intent = (item.intent or profile.intent or "commercial").lower()
    lo, hi = LINKS_BY_INTENT.get(intent, LINKS_BY_INTENT["commercial"])
    n_internal = len(item.internal_links)

    if item.internal_links or item.external_links or item.body:
        if n_internal < lo:
            f.append(Finding(
                "internal-links-below-min", "warn", item.ref,
                f"{_plural(n_internal, 'internal link')} for a {intent} page; minimum is {lo}.",
            ))
        elif n_internal > hi:
            f.append(Finding(
                "internal-links-above-max", "info", item.ref,
                f"{n_internal} internal links for a {intent} page; ceiling is {hi}.",
            ))

        n_external = len(item.external_links)
        if n_external > EXTERNAL_LINKS_MAX:
            f.append(Finding(
                "external-links-over-cap", "warn", item.ref,
                f"{n_external} external links; cap is {EXTERNAL_LINKS_MAX} "
                f"unless the brief approves more.",
            ))

    generic_anchors = {"click here", "read more", "learn more"}
    lexicon = profile.lexicon
    if lexicon is not None:
        generic_anchors = generic_anchors | lexicon.anchor_generic

    anchor_counts: dict[str, int] = {}
    for link in item.internal_links:
        anchor = link.split("(", 1)[0].strip().lower()
        if anchor:
            anchor_counts[anchor] = anchor_counts.get(anchor, 0) + 1
    for anchor, count in anchor_counts.items():
        if count >= 3:
            f.append(Finding(
                "anchor-overoptimized", "warn", item.ref,
                f"Anchor {anchor!r} reused {count} times.",
                value=anchor,
            ))
        if anchor in generic_anchors:
            f.append(Finding(
                "anchor-generic", "info", item.ref,
                f"Generic anchor text: {anchor!r}.",
                value=anchor,
            ))

    return f


# ── E: BRAND VOICE (правила, яких немає в policy.py) ──────────────────────────


def check_brand_voice_own(item: ContentItem, profile: Profile) -> list[Finding]:
    f: list[Finding] = []
    lexicon = profile.lexicon
    is_english = profile.language == "en"

    # Дублі — не мовно-специфічні (терміни агенції, не природна мова) — як є.
    # Contractions: англ. апострофні форми лише для "en"; для іншої мови —
    # лексиконовий regex розмовних форм, якщо він є (task-87 §1.2).
    contractions_re = (
        CONTRACTIONS_RE if is_english
        else (lexicon.contractions_re if lexicon else None)
    )
    # US/GB spelling має сенс лише для англійської — на живому шведському
    # копірайті стріляв у слова "program"/"favorite" (task-87 §0.2).
    variants = (
        (US_GB_SPELLING if profile.locale == "en-GB"
         else {v: k for k, v in US_GB_SPELLING.items()})
        if is_english else {}
    )
    word_swaps = WORD_SWAPS if is_english else (lexicon.word_swaps if lexicon else {})

    for field_name, text in _fields(item):
        low = text.lower()
        value = excerpt(text)

        for banned, canonical in TERMINOLOGY_BANNED.items():
            if re.search(rf"\b{re.escape(banned)}\b", low):
                f.append(Finding(
                    "voice-terminology", "error", item.ref,
                    f"{field_name}: {banned!r} used; house term is {canonical!r}.",
                    value=value,
                ))

        if _money_leak_hit(text, lexicon):
            f.append(Finding(
                "voice-money-leak", "error", item.ref,
                f"{field_name}: a fee/salary figure appears to be exposed.",
                value=value,
            ))

        if profile.kind != "email" and contractions_re and contractions_re.search(text):
            f.append(Finding(
                "voice-contraction-in-formal", "warn", item.ref,
                f"{field_name}: contraction in a formal deliverable.",
                value=value,
            ))

        for wrong, right in variants.items():
            if re.search(rf"\b{re.escape(wrong)}\w*\b", low):
                f.append(Finding(
                    "voice-locale-mismatch", "warn", item.ref,
                    f"{field_name}: {wrong!r} does not match locale {profile.locale} "
                    f"(expected {right!r}-family spelling).",
                    value=value,
                ))

        for bad, suggestion in word_swaps.items():
            if bad in low:
                f.append(Finding(
                    "voice-word-swap", "info", item.ref,
                    f"{field_name}: {bad!r} -> {suggestion}",
                    value=value,
                ))

    return f


def _money_leak_hit(text: str, lexicon: Lexicon | None) -> bool:
    """Тригер-слово + валюта — обидві сторони можуть прийти або з
    англійського дефолту, або з лексикону мови: документ, що леакає
    гонорар англійським словом у шведському тексті (чи навпаки), усе одно
    має спрацювати (task-87 §1.2 — money-leak рядок)."""
    trigger = bool(MONEY_TRIGGER_RE.search(text)) or bool(
        lexicon and lexicon.money_trigger_re and lexicon.money_trigger_re.search(text)
    )
    currency = bool(CURRENCY_RE.search(text)) or bool(
        lexicon and lexicon.money_currency_re and lexicon.money_currency_re.search(text)
    )
    return trigger and currency


# ── D: SLOP поза AI_SLOP_PHRASES ───────────────────────────────────────────


def check_wikipedia_slop(item: ContentItem, profile: Profile) -> list[Finding]:
    f: list[Finding] = []
    doc_text_parts: list[str] = []
    lexicon = profile.lexicon
    is_english = profile.language == "en"

    negative_parallelism_re = (
        NEGATIVE_PARALLELISM_RE if is_english
        else (lexicon.negative_parallelism_re if lexicon else None)
    )
    copula_avoidance_phrases = (
        COPULA_AVOIDANCE_PHRASES if is_english
        else (lexicon.copula_avoidance_phrases if lexicon else ())
    )
    vague_attribution_phrases = (
        VAGUE_ATTRIBUTION_PHRASES if is_english
        else (lexicon.vague_attribution_phrases if lexicon else ())
    )
    collaborative_voice_phrases = (
        COLLABORATIVE_VOICE_PHRASES if is_english
        else (lexicon.collaborative_voice_phrases if lexicon else ())
    )
    # Participle-chain, undue-significance і rule-of-three (нижче) лишаються
    # чисто англійськими синтаксичними явищами — немає шведського
    # відповідника в task-87 §1.2, лейн просто не спрацьовує для інших мов
    # (видно в locale-rules-adjusted, engine.py).

    for field_name, text in _fields(item):
        doc_text_parts.append(text)
        low = text.lower()
        value = excerpt(text)

        if negative_parallelism_re and negative_parallelism_re.search(text):
            f.append(Finding(
                "slop-negative-parallelism", "info", item.ref,
                f"{field_name}: 'not just/only X but Y' construction.",
                value=value,
            ))

        for phrase in _phrase_hits(text, copula_avoidance_phrases):
            f.append(Finding(
                "slop-copula-avoidance", "info", item.ref,
                f"{field_name}: {phrase!r} avoids a plain 'is/are'.",
                value=value,
            ))

        for phrase in _phrase_hits(text, vague_attribution_phrases):
            f.append(Finding(
                "slop-vague-attribution", "warn", item.ref,
                f"{field_name}: {phrase!r} names no actual source.",
                value=value,
            ))

        if is_english and PARTICIPLE_CHAIN_RE.search(text):
            f.append(Finding(
                "slop-participle-chain", "info", item.ref,
                f"{field_name}: trailing participle clause adds no evidence.",
                value=value,
            ))

        if is_english:
            for phrase in _phrase_hits(text, UNDUE_SIGNIFICANCE_PHRASES):
                f.append(Finding(
                    "slop-undue-significance", "info", item.ref,
                    f"{field_name}: {phrase!r} inflates importance without a fact.",
                    value=value,
                ))

        for phrase in _phrase_hits(text, collaborative_voice_phrases):
            f.append(Finding(
                "slop-collaborative-voice", "warn", item.ref,
                f"{field_name}: {phrase!r} breaks third-person deliverable voice.",
                value=value,
            ))

        for pat in LLM_ARTIFACT_PATTERNS:
            if pat in low:
                f.append(Finding(
                    "slop-llm-artifact", "error", item.ref,
                    f"{field_name}: leaked LLM tool markup {pat!r}.",
                    value=value,
                ))
        if LLM_ARTIFACT_CITE_RE.search(text):
            f.append(Finding(
                "slop-llm-artifact", "error", item.ref,
                f"{field_name}: leaked LLM citation markup.",
                value=value,
            ))

        for pat in PLACEHOLDER_PATTERNS:
            if pat in low:
                f.append(Finding(
                    "slop-placeholder", "error", item.ref,
                    f"{field_name}: unresolved placeholder {pat!r}.",
                    value=value,
                ))

        if EMOJI_RE.search(text):
            f.append(Finding(
                "slop-emoji-formatting", "error", item.ref,
                f"{field_name}: emoji used as a formatting marker.",
                value=value,
            ))

    if is_english:
        doc_text = " ".join(doc_text_parts)
        if len(RULE_OF_THREE_RE.findall(doc_text)) > RULE_OF_THREE_DOC_THRESHOLD:
            f.append(Finding(
                "slop-rule-of-three", "info", item.ref,
                "Triadic 'X, Y, and Z' lists appear more than 3 times.",
            ))

    return f


# ── G: DATA INTEGRITY ──────────────────────────────────────────────────────


def check_data_integrity(item: ContentItem, profile: Profile) -> list[Finding]:
    f: list[Finding] = []
    if item.page_name is not None and item.page_name.strip() and (
        (item.title is None or item.title == "")
        and (item.description is None or item.description == "")
        and (item.h1 is None or item.h1 == "")
    ):
        f.append(Finding(
            "row-incomplete", "warn", item.ref,
            f"{item.page_name!r} has a name but no title, description or H1.",
            value=item.page_name,
        ))
    return f


def check_cross_page(doc: ContentDoc, profile: Profile) -> list[Finding]:
    """Дублі URL і канібалізація ключів — за замовчуванням у межах аркуша,
    не по всьому документу (task-87 §1.3): сторінка, що є і в Q2-, і в
    Q3-стратегії того самого файлу, — історія планування, не помилка.
    `Profile.cross_sheet=True` (дефолт — сумісність зі звітами до task-87)
    повертає старе поводження свідомо."""
    f: list[Finding] = []
    for group in _sheet_groups(doc.items, profile):
        by_url: dict[str, list[ContentItem]] = {}
        by_keyword: dict[str, list[ContentItem]] = {}
        for item in group:
            if item.url:
                by_url.setdefault(item.url, []).append(item)
            if item.keywords:
                primary = item.keywords[0].strip().lower()
                if primary:
                    by_keyword.setdefault(primary, []).append(item)

        for url, items in by_url.items():
            if len(items) > 1:
                refs = ", ".join(i.ref for i in items)
                f.append(Finding(
                    "url-duplicate", "error", refs,
                    f"URL {url!r} appears on {len(items)} rows: {refs}",
                    value=url,
                ))

        for kw, items in by_keyword.items():
            if len(items) > 1:
                refs = ", ".join(i.ref for i in items)
                f.append(Finding(
                    "keyword-duplicate-across-pages", "error", refs,
                    f"Primary keyword {kw!r} targeted by {len(items)} pages "
                    f"(cannibalization risk): {refs}",
                    value=kw,
                ))

    return f


def _sheet_groups(
    items: tuple[ContentItem, ...], profile: Profile
) -> list[list[ContentItem]]:
    """Групує items за `item.sheet` для крос-сторінкових перевірок, якщо
    `profile.cross_sheet` вимкнено; інакше — один список, як до task-87.
    DOCX-документи (`sheet is None` для всіх) завжди дають одну групу."""
    if profile.cross_sheet:
        return [list(items)]
    groups: dict[str | None, list[ContentItem]] = {}
    for item in items:
        groups.setdefault(item.sheet, []).append(item)
    return list(groups.values())
