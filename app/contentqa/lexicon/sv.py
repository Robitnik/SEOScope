"""Шведський лексикон (task-87 §1.2).

Списки калібровані на живому портфелі — `laxton.se` (174 сторінки з
`monitor_pages`, LAXTON_SEO_STRATEGY workbook, 185 items) під час
дослідження task-87. Не переклад англійських списків слово-в-слово: кожен
пункт або (а) справжній AI-slop-паттерн шведською, або (б) хибне
спрацювання, знайдене на живих даних (en dash, US/GB spelling, читабельність
— див. коментарі нижче й PLAN §12 / task-87 §0.2).
"""

from __future__ import annotations

import re

from app.contentqa.lexicon import Lexicon

# Генерик-опенери — той самий клас, що англійський BANNED_OPENERS
# (розвідка/imperative CTA як перше слово опису).
_BANNED_OPENERS_SV: frozenset[str] = frozenset({
    "upptäck", "utforska", "boka", "köp", "handla", "beställ", "läs", "se",
    "hitta", "välkommen", "kontakta", "ring", "prova", "spara", "njut",
    "registrera", "ladda", "besök", "jämför", "välj", "förvandla", "höj",
    "få", "ta",
})

_AI_SLOP_PHRASES_SV: frozenset[str] = frozenset({
    "i dagens digitala värld", "oavsett om du", "sök inte längre",
    "vi har allt du behöver", "när det kommer till", "ett brett utbud av",
    "skräddarsydd efter dina behov", "ta första steget",
    "det är viktigt att notera", "i takt med att",
    "spelar en viktig roll", "hos oss får du",
})

_FILLER_TERMS_SV: frozenset[str] = frozenset({
    "marknadsledande", "branschledande", "helhetslösning", "kvalitetssäkrad",
    "högkvalitativ", "prisvärd", "professionell", "erfaren", "dedikerad",
    "omfattande", "toppmodern", "banbrytande", "sömlös", "skräddarsydd",
    "unik", "i världsklass", "förstklassig", "oslagbar",
})

# klarspråk ("plain language") word-swaps — шведський аналог WORD_SWAPS.
_WORD_SWAPS_SV: dict[str, str] = {
    "nyttja": "använda",
    "i syfte att": "för att",
    "erhålla": "få",
    "föreligger": "finns",
    "avseende": "om",
    "påbörja": "börja",
    "verkställa": "göra",
    "medelst": "med",
    "innehava": "ha",
    "vidtaga åtgärder": "göra",
}

_ANCHOR_GENERIC_SV: frozenset[str] = frozenset({"läs mer", "här", "klicka här"})

# Функціональні слова, які не несуть топікового сигналу для keyword-in-title
# стем-матчу (policy_rules._keyword_in_title_stem) — не намагаємось повторити
# AISeoTools' _TOPIC_STOPWORDS (англійський), пишемо свій маленький список.
_STOPWORDS_SV: frozenset[str] = frozenset({
    "och", "är", "för", "med", "till", "av", "som", "en", "ett", "den", "det",
    "på", "i", "att", "du", "jag", "vi", "vår", "era", "hos", "från", "kan",
    "blir", "bli", "har", "hade", "inte", "men", "eller", "så", "om", "ska",
    "skall", "vill", "får", "ha", "de", "man",
})

# Розмовні скорочені форми у формальному документі — не англ. contractions
# (apostrophe forms), а шведські розмовні варіанти повних слів.
_CONTRACTIONS_RE_SV = re.compile(
    r"\b(nån|nåt|sen|dom|va|sån|mycke)\b", re.IGNORECASE
)

# Wikipedia-слоп: лише те, що реально перекладається як синтаксична
# конструкція (не буквальний переклад англ. фрази) — рядок 1.2 плану:
# participle-chain/undue-significance/rule-of-three лишаються англійськими
# синтаксичними явищами й не мають шведського відповідника тут (disabled_lanes).
_NEGATIVE_PARALLELISM_RE_SV = re.compile(
    r"\binte bara\b.{0,40}\butan också\b", re.IGNORECASE
)
_COPULA_AVOIDANCE_PHRASES_SV = ("utgör en", "utgör ett")
_VAGUE_ATTRIBUTION_PHRASES_SV = ("enligt vissa experter",)
_COLLABORATIVE_VOICE_PHRASES_SV = ("som vi ser", "det är värt att notera")

# money-leak: валютні позначення + тригер-слова. Без другого без першого
# перевірка не спрацює (rules.check_brand_voice_own вимагає ОБОХ) — тому
# лексикон несе власну пару, а не лише розширення символів.
_MONEY_TRIGGER_RE_SV = re.compile(
    r"\b(avgift|lön|arvode|provision|timpris|lönespec|kostnad)\b", re.IGNORECASE
)
_MONEY_CURRENCY_RE_SV = re.compile(
    r"[$£€]\s?\d|\bkr\b|\bSEK\b|\d+\s?:-|\b\d+(\.\d+)?\s?(usd|gbp|eur|sek)\b",
    re.IGNORECASE,
)

LEXICON = Lexicon(
    code="sv",
    name="Swedish",
    banned_openers=_BANNED_OPENERS_SV,
    ai_slop_phrases=_AI_SLOP_PHRASES_SV,
    filler_terms=_FILLER_TERMS_SV,
    word_swaps=_WORD_SWAPS_SV,
    anchor_generic=_ANCHOR_GENERIC_SV,
    stopwords=_STOPWORDS_SV,
    contractions_re=_CONTRACTIONS_RE_SV,
    negative_parallelism_re=_NEGATIVE_PARALLELISM_RE_SV,
    copula_avoidance_phrases=_COPULA_AVOIDANCE_PHRASES_SV,
    vague_attribution_phrases=_VAGUE_ATTRIBUTION_PHRASES_SV,
    collaborative_voice_phrases=_COLLABORATIVE_VOICE_PHRASES_SV,
    money_trigger_re=_MONEY_TRIGGER_RE_SV,
    money_currency_re=_MONEY_CURRENCY_RE_SV,
    # en dash — стандартний шведський tankstreck (виміряно: 12 en dash / 0 em
    # dash на живому laxton.se копірайті; воркбук дав 136 хибних
    # voice-em-dash саме через це, task-87 §0.2).
    report_en_dash=False,
    # LIX (Björnsson), не Flesch-Kincaid: калібровано на 141 живому описі
    # laxton.se (медіана 34.8, p10 22.0, p90 45.0) — task-87 §0.2.5.
    readability_min=30.0,
    readability_max=45.0,
    disabled_lanes=(
        "US/GB spelling (English-only word list)",
        "participle-chain slop pattern (English syntax)",
        "undue-significance slop phrases (English syntax)",
        "rule-of-three slop detector (requires English \"and\")",
        "keyword-in-title: exact match replaced with a stem match "
        "(Swedish compounding/definite-form endings)",
    ),
)
