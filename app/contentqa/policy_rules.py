"""Правила, що спираються на `seo_audit.policy` через policy_bridge.py:
meta-довжини, AI_SLOP_PHRASES/BANNED_OPENERS/FILLER_TERMS, em-dash,
process-leak, tool-failure narrative, keyword-in-title, title/desc-дублі.

Кожна функція тут може кинути PolicyUnavailable — виклик обгортається один
раз в engine.py (`policy_bridge.probe()`), не на кожному ContentItem.
"""

from __future__ import annotations

import re
import unicodedata

from app.contentqa import policy_bridge as pb
from app.contentqa.findings import Finding, excerpt
from app.contentqa.model import ContentDoc, ContentItem
from app.contentqa.rules import DESC_MIN, TITLE_MIN, Profile, _fields, _phrase_hits, _sheet_groups

_WORD_RE = re.compile(r"[^\W\d_]+", re.UNICODE)
# Мінімальна довжина спільного префікса (символів) і частка від коротшого
# токена — та сама ідея, що AISeoTools' `_related_topic_token`
# (policy.py), але з нижчою планкою: той поріг (5 спільних символів)
# калібрований на довших англійських/російських коренях і майже дорівнює
# точному збігу для коротких шведських слів (`mössor` vs `Mössa` дає лише
# 4 спільні символи до розбіжності — виміряно в task-87 §0.2, звідси й
# власний, а не перенесений з AISeoTools поріг).
_STEM_MIN_COMMON = 3
_STEM_MIN_RATIO = 0.6


def _fold(text: str) -> str:
    return unicodedata.normalize("NFKC", text or "").casefold()


def _content_words(text: str, stopwords: frozenset[str]) -> list[str]:
    return [w for w in _WORD_RE.findall(_fold(text)) if len(w) > 2 and w not in stopwords]


def _related_stem(a: str, b: str) -> bool:
    if a == b:
        return True
    common = 0
    for x, y in zip(a, b, strict=False):  # довжини навмисно різні — рахуємо спільний префікс
        if x != y:
            break
        common += 1
    shorter = min(len(a), len(b))
    if shorter <= 3:
        return False
    return common >= _STEM_MIN_COMMON and common / shorter >= _STEM_MIN_RATIO


def _keyword_in_title(title: str, keyword: str, profile: Profile) -> bool:
    """`pb.keyword_in_title` для англійської (як завжди); для іншої мови —
    власний стем-матч (task-87 §1.2): точний токен-збіг ламався на
    шведській морфології (`mössor` проти `Mössa …` -> хибний
    `meta-title-keyword-missing`, task-87 §0.2)."""
    if profile.language == "en":
        return pb.keyword_in_title(title, keyword)
    lexicon = profile.lexicon
    stopwords = lexicon.stopwords if lexicon else frozenset()
    title_words = _content_words(title, stopwords)
    keyword_words = _content_words(keyword, stopwords)
    if not title_words or not keyword_words:
        return True  # немає змістовних токенів для звірки — той самий "None -> True", що pb
    return any(_related_stem(a, b) for a in keyword_words for b in title_words)


def _first_word(text: str) -> str:
    match = _WORD_RE.search(_fold(text or ""))
    return match.group(0) if match else ""


def _banned_opener(text: str, profile: Profile) -> str | None:
    """`pb.banned_opener` для англійської; лексиконовий список — для іншої
    мови (task-87 §1.2). `None`, коли для мови нема лексикону — не тиша:
    engine.py фіксує це в `locale-rules-adjusted`."""
    if profile.language == "en":
        return pb.banned_opener(text)
    lexicon = profile.lexicon
    if not lexicon:
        return None
    word = _first_word(text)
    return word if word in lexicon.banned_openers else None


def _filler_terms(text: str, profile: Profile) -> list[str]:
    if profile.language == "en":
        return pb.filler_terms(text)
    lexicon = profile.lexicon
    if not lexicon or not text:
        return []
    # Підрядковий, не whole-word збіг: на відміну від англійського
    # FILLER_TERMS (лише однослівні/дефісні записи), шведський список
    # містить і короткі фрази ("i världsklass") — той самий інструмент,
    # що AI_SLOP_PHRASES нижче.
    return _phrase_hits(text, tuple(lexicon.filler_terms))


def _slop_phrases(text: str, profile: Profile) -> list[str]:
    if profile.language == "en":
        return pb.slop_phrases(text)
    lexicon = profile.lexicon
    if not lexicon:
        return []
    return _phrase_hits(text, tuple(lexicon.ai_slop_phrases))


def _em_dash_hits(text: str, profile: Profile) -> list[str]:
    """`pb.em_dash_hits` називає і em, і en dash — коректно для англійської,
    але en dash — стандартна пунктуація частини мов (шведський tankstreck:
    136 хибних `voice-em-dash` на живому LAXTON-воркбуці, task-87 §0.2).
    Лексикон із `report_en_dash=False` фільтрує "en dash" з результату; em
    dash лишається error для будь-якої мови."""
    hits = pb.em_dash_hits(text)
    lexicon = profile.lexicon
    if lexicon is not None and not lexicon.report_en_dash:
        hits = [h for h in hits if h != "en dash"]
    return hits


def check_meta(item: ContentItem, profile: Profile) -> list[Finding]:
    f: list[Finding] = []

    if item.title == "":
        f.append(Finding(
            "meta-title-missing", "error", item.ref,
            "Title field is present and empty.",
        ))
    elif item.title:
        tl = len(item.title)
        title_max = pb.title_max()
        title_value = excerpt(item.title)
        if tl > title_max:
            f.append(Finding(
                "meta-title-length", "error", item.ref,
                f"Title is {tl} chars; ceiling is {title_max}.",
                value=title_value,
            ))
        elif tl < TITLE_MIN:
            f.append(Finding(
                "meta-title-length", "warn", item.ref,
                f"Title is {tl} chars; floor is {TITLE_MIN}.",
                value=title_value,
            ))

        for brand in profile.brand_names:
            suffix = f"| {brand}"
            if item.title.strip().endswith(suffix) and tl > title_max:
                f.append(Finding(
                    "meta-title-brand-padding", "error", item.ref,
                    f"Title exceeds {title_max} chars and ends with {suffix!r} "
                    f"— brand padding pushed it over.",
                    value=title_value,
                ))

        if item.keywords and not _keyword_in_title(item.title, item.keywords[0], profile):
            f.append(Finding(
                "meta-title-keyword-missing", "warn", item.ref,
                f"Primary keyword {item.keywords[0]!r} not found in title.",
                value=title_value,
            ))

    if item.description == "":
        f.append(Finding(
            "meta-desc-missing", "error", item.ref,
            "Description field is present and empty.",
        ))
    elif item.description:
        dl = len(item.description)
        desc_max = pb.desc_max()
        desc_value = excerpt(item.description)
        if dl > desc_max:
            f.append(Finding(
                "meta-desc-length", "error", item.ref,
                f"Description is {dl} chars; ceiling is {desc_max}.",
                value=desc_value,
            ))
        elif dl < DESC_MIN:
            f.append(Finding(
                "meta-desc-length", "warn", item.ref,
                f"Description is {dl} chars; floor is {DESC_MIN}.",
                value=desc_value,
            ))
        elif dl > pb.desc_authored_max():
            f.append(Finding(
                "meta-desc-authored-long", "info", item.ref,
                f"Description is {dl} chars; Google renders ~{pb.desc_authored_max()}, "
                f"tighten if freshly written.",
                value=desc_value,
            ))

        opener = _banned_opener(item.description, profile)
        if opener:
            f.append(Finding(
                "meta-desc-banned-opener", "error", item.ref,
                f"Description opens with banned opener {opener!r}.",
                value=desc_value,
            ))

    return f


def check_slop_and_voice_policy(item: ContentItem, profile: Profile) -> list[Finding]:
    f: list[Finding] = []
    for field_name, text in _fields(item):
        value = excerpt(text)
        for phrase in _slop_phrases(text, profile):
            f.append(Finding(
                "slop-phrase", "warn", item.ref,
                f"{field_name}: {phrase!r} is interchangeable AI-marketing copy.",
                value=value,
            ))
        for term in _filler_terms(text, profile):
            f.append(Finding(
                "slop-filler-term", "warn", item.ref,
                f"{field_name}: {term!r} asserts quality without proving it.",
                value=value,
            ))
        for dash in _em_dash_hits(text, profile):
            f.append(Finding(
                "voice-em-dash", "error", item.ref,
                f"{field_name}: {dash} present — house rule bans it.",
                value=value,
            ))
        # process-leak/tool-failure-narrative — мовно-нейтральні: назви наших
        # інструментів, наші артефакти й англійські фрази-провали процесу
        # леакають у документ будь-якою мовою так само (task-87 §1.2).
        for hit in pb.process_leak_hits(text):
            f.append(Finding(
                "process-leak", "error", item.ref,
                f"{field_name}: {hit!r} names our tooling/process, not the page.",
                value=value,
            ))
        for hit in pb.tool_failure_phrases(text):
            f.append(Finding(
                "tool-failure-narrative", "error", item.ref,
                f"{field_name}: {hit!r} leaks a research-session failure into copy.",
                value=value,
            ))
    return f


def check_meta_duplicates(doc: ContentDoc, profile: Profile) -> list[Finding]:
    """Дублі title/description — той самий скоуп-за-аркушем, що
    `check_cross_page` (task-87 §1.3): `Profile.cross_sheet=False` порівнює
    лише в межах обраного аркуша."""
    f: list[Finding] = []
    for group in _sheet_groups(doc.items, profile):
        items = [i for i in group if i.title or i.description]
        for i in range(len(items)):
            for j in range(i + 1, len(items)):
                a, b = items[i], items[j]
                if a.title and b.title:
                    sim = pb.text_similarity(a.title, b.title)
                    if sim >= pb.title_similarity_max():
                        f.append(Finding(
                            "meta-title-duplicate", "error", f"{a.ref}, {b.ref}",
                            f"Titles {sim:.2f} similar: {a.title!r} / {b.title!r}",
                            value=excerpt(a.title),
                        ))
                if a.description and b.description:
                    sim = pb.text_similarity(a.description, b.description)
                    if sim >= pb.description_similarity_max():
                        f.append(Finding(
                            "meta-desc-duplicate", "error", f"{a.ref}, {b.ref}",
                            f"Descriptions {sim:.2f} similar.",
                            value=excerpt(a.description),
                        ))
    return f
