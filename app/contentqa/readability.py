"""Flesch–Kincaid Grade Level — власна реалізація замість `textstat` (тягне
`nltk`+`pyphen`, а `poetry lock` у пісочниці заблокований, див. пам'ять
env-poetry-lock-limitation). Підрахунок складів — за групами голосних, що є
стандартним наближенням для формули (сама формула точна лише настільки,
наскільки точний підрахунок складів; це прийнятний компроміс і для Hemingway,
і для Flesch–Kincaid, які саме на ньому й побудовані).
"""

from __future__ import annotations

import re

from app.contentqa.findings import Finding, excerpt
from app.contentqa.model import ContentItem
from app.contentqa.rules import (
    HEMINGWAY_GRADE_MAX,
    HEMINGWAY_GRADE_MIN,
    SENTENCE_MAX_WORDS,
    Profile,
)

_SENTENCE_SPLIT_RE = re.compile(r"[.!?]+(?:\s|$)")
# ASCII-only — навмисно: syllable-підрахунок нижче (голосні групи a/e/i/o/u/y)
# сам є англійською фонетичною евристикою, тож токенізація для нього так само
# англійська. Використовувати лише для Flesch-Kincaid, не для загального
# підрахунку слів (той — `_UNICODE_WORD_RE` нижче).
_WORD_RE = re.compile(r"[A-Za-z']+")
# Unicode-safe — для LIX (task-87 §1.2: шведські å/ä/ö мають рахуватись у
# слові й впливати на "довге слово", інакше ASCII-регекс ріже "är" на "r" і
# системно занижує LIX на реальному шведському тексті) і для загального
# "скільки слів у реченні" (sentence-too-long нижче) — той самий підхід, що
# `rules._WORD_RE`.
_UNICODE_WORD_RE = re.compile(r"[^\W\d_]+", re.UNICODE)
_VOWEL_GROUP_RE = re.compile(r"[aeiouy]+", re.IGNORECASE)


def count_syllables(word: str) -> int:
    word = word.lower().strip("'")
    if not word:
        return 0
    groups = _VOWEL_GROUP_RE.findall(word)
    n = len(groups)
    if word.endswith("e") and not word.endswith("le") and n > 1:
        n -= 1
    return max(n, 1)


def flesch_kincaid_grade(text: str) -> float | None:
    sentences = [s for s in _SENTENCE_SPLIT_RE.split(text or "") if s.strip()]
    words = _WORD_RE.findall(text or "")
    if not sentences or not words:
        return None
    syllables = sum(count_syllables(w) for w in words)
    return round(0.39 * (len(words) / len(sentences)) + 11.8 * (syllables / len(words)) - 15.59, 2)


_LIX_LONG_WORD_MIN_LEN = 6  # Björnsson: слово довше 6 символів рахується "довгим"


def lix(text: str) -> float | None:
    """LIX (Björnsson) — стандартна формула читабельності для шведської та
    інших скандинавських мов: Flesch-Kincaid рахує склади через голосні
    групи, калібрований під англійську фонетику, і дає оманливо "добрий"
    результат на канцелярській шведській прозі (9.38 на живому тексті
    laxton.se — усередині англійського банду 7-10, тоді як LIX того самого
    тексту — 54.8, важкий; task-87 §0.2.3). LIX = слова/речення +
    100 × довгі_слова / слова."""
    sentences = [s for s in _SENTENCE_SPLIT_RE.split(text or "") if s.strip()]
    words = _UNICODE_WORD_RE.findall(text or "")
    if not sentences or not words:
        return None
    long_words = sum(1 for w in words if len(w) > _LIX_LONG_WORD_MIN_LEN)
    return round(len(words) / len(sentences) + 100 * long_words / len(words), 2)


def check_readability(item: ContentItem, profile: Profile) -> list[Finding]:
    f: list[Finding] = []
    if not item.body:
        return f

    lexicon = profile.lexicon
    if profile.language == "en":
        grade = flesch_kincaid_grade(item.body)
        if grade is not None and not (HEMINGWAY_GRADE_MIN <= grade <= HEMINGWAY_GRADE_MAX):
            f.append(Finding(
                "readability-grade", "warn", item.ref,
                f"Flesch-Kincaid grade {grade}; house band is "
                f"{HEMINGWAY_GRADE_MIN}-{HEMINGWAY_GRADE_MAX}.",
                value=excerpt(item.body),
            ))
    elif (
        lexicon is not None
        and lexicon.readability_min is not None
        and lexicon.readability_max is not None
    ):
        score = lix(item.body)
        if score is not None and not (lexicon.readability_min <= score <= lexicon.readability_max):
            f.append(Finding(
                "readability-grade", "warn", item.ref,
                f"LIX {score}; {lexicon.name} house band is "
                f"{lexicon.readability_min}-{lexicon.readability_max}.",
                value=excerpt(item.body),
            ))
    # Немає лексикону з каліброваним банду для цієї мови — лейн не
    # спрацьовує (не мовчазна "чистота": фіксується в locale-rules-adjusted).

    for sentence in _SENTENCE_SPLIT_RE.split(item.body):
        n = len(_UNICODE_WORD_RE.findall(sentence))
        if n > SENTENCE_MAX_WORDS:
            f.append(Finding(
                "sentence-too-long", "info", item.ref,
                f"{n}-word sentence (limit {SENTENCE_MAX_WORDS}).",
                value=excerpt(sentence.strip()),
            ))

    return f
