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
_WORD_RE = re.compile(r"[A-Za-z']+")
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


def check_readability(item: ContentItem, profile: Profile) -> list[Finding]:
    f: list[Finding] = []
    if not item.body:
        return f

    grade = flesch_kincaid_grade(item.body)
    if grade is not None and not (HEMINGWAY_GRADE_MIN <= grade <= HEMINGWAY_GRADE_MAX):
        f.append(Finding(
            "readability-grade", "warn", item.ref,
            f"Flesch-Kincaid grade {grade}; house band is "
            f"{HEMINGWAY_GRADE_MIN}-{HEMINGWAY_GRADE_MAX}.",
            value=excerpt(item.body),
        ))

    for sentence in _SENTENCE_SPLIT_RE.split(item.body):
        n = len(_WORD_RE.findall(sentence))
        if n > SENTENCE_MAX_WORDS:
            f.append(Finding(
                "sentence-too-long", "info", item.ref,
                f"{n}-word sentence (limit {SENTENCE_MAX_WORDS}).",
                value=excerpt(sentence.strip()),
            ))

    return f
