"""Мовний реєстр для content-qa (task-87).

Англійські списки (banned openers, AI-slop фрази, filler-терміни, дублі,
keyword-helpers) живуть в AISeoTools `seo_audit.policy` і йдуть через
`policy_bridge.py` — AISeoTools сам генерує англійські стратегії й перевіряє
власний вивід (task-87 §1.1). Кожна наступна мова, якою AISeoTools нічого не
виробляє, отримує свій модуль тут, а не в сусідньому репо: єдиний споживач
цих списків — цей дашборд.

`Lexicon` — не Protocol/ABC, а звичайний frozen dataclass із дефолтами:
нова мова описує лише те, чим вона відрізняється від "нічого не робимо",
решта полів лишається порожньою (лейн просто не спрацює для цієї мови, і це
видно в `disabled_lanes` — engine.py зводить це в один `locale-rules-adjusted`
finding, PLAN §12: не перевірили ≠ порушень немає).

Реєстр навмисно НЕ автозавантажує всі модулі одразу (щоб додавання нової
мови ніколи не вимагало правок тут) — `get_lexicon()` імпортує лінькво.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Lexicon:
    code: str  # ISO 639-1, напр. "sv" — Profile.language звідси
    name: str  # людська назва для тексту locale-rules-adjusted, напр. "Swedish"

    banned_openers: frozenset[str] = frozenset()
    ai_slop_phrases: frozenset[str] = frozenset()
    filler_terms: frozenset[str] = frozenset()
    word_swaps: dict[str, str] = field(default_factory=dict)
    anchor_generic: frozenset[str] = frozenset()

    stopwords: frozenset[str] = frozenset()  # для keyword-in-title стем-матчу

    contractions_re: re.Pattern[str] | None = None
    negative_parallelism_re: re.Pattern[str] | None = None
    copula_avoidance_phrases: tuple[str, ...] = ()
    vague_attribution_phrases: tuple[str, ...] = ()
    collaborative_voice_phrases: tuple[str, ...] = ()

    money_trigger_re: re.Pattern[str] | None = None
    money_currency_re: re.Pattern[str] | None = None

    # en dash — стандартна пунктуація в частині мов (шведський tankstreck):
    # False вимикає лише en-dash-репортинг, em dash лишається error завжди.
    report_en_dash: bool = True

    readability_min: float | None = None  # None -> формула для цієї мови не застосовується
    readability_max: float | None = None

    # Людський опис того, що для цієї мови НЕ перевіряється (англійські
    # синтаксичні лейни на кшталт participle-chain/rule-of-three) —
    # потрапляє дослівно в один `locale-rules-adjusted` finding.
    disabled_lanes: tuple[str, ...] = ()


def get_lexicon(language: str) -> Lexicon | None:
    """`None` для "en" (веде через policy_bridge, не через реєстр) і для
    будь-якої мови, для якої лексикону ще не написано."""
    if language == "sv":
        from app.contentqa.lexicon import sv

        return sv.LEXICON
    return None
