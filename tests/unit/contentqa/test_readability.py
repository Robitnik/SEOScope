from __future__ import annotations

from app.contentqa.model import ContentItem
from app.contentqa.readability import check_readability, count_syllables, flesch_kincaid_grade
from app.contentqa.rules import Profile


def test_count_syllables_basic_words() -> None:
    assert count_syllables("cat") == 1
    assert count_syllables("condition") >= 3
    assert count_syllables("the") == 1


def test_flesch_kincaid_grade_none_when_no_text() -> None:
    assert flesch_kincaid_grade("") is None
    assert flesch_kincaid_grade(None) is None  # type: ignore[arg-type]


def test_flesch_kincaid_grade_simple_sentence_is_low_grade() -> None:
    text = "The cat sat on the mat. It was a hot day."
    grade = flesch_kincaid_grade(text)
    assert grade is not None
    assert grade < 7.0


def test_readability_not_checked_when_body_is_none() -> None:
    item = ContentItem(ref="r", body=None)
    assert check_readability(item, Profile()) == []


def test_sentence_too_long_flagged() -> None:
    long_sentence = " ".join(["word"] * 35) + "."
    item = ContentItem(ref="r", body=long_sentence)
    findings = check_readability(item, Profile())
    assert any(f.check == "sentence-too-long" for f in findings)
