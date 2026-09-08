from __future__ import annotations

from app.contentqa.model import ContentItem
from app.contentqa.readability import check_readability, count_syllables, flesch_kincaid_grade, lix
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


# ── task-87 §1.2: LIX (шведська), не Flesch-Kincaid ──────────────────────────


def test_lix_none_when_no_text() -> None:
    assert lix("") is None
    assert lix(None) is None  # type: ignore[arg-type]


def test_lix_formula_on_a_known_short_text() -> None:
    # 2 слова / 1 речення + 100 * 1 довге(>6 символів) слово / 2 слова = 2 + 50 = 52.
    assert lix("abcd abcdefgh.") == 52.0


def test_lix_counts_swedish_letters_as_part_of_the_word() -> None:
    # ASCII-only word regex ріже "är" на "r" і губить довжину слів із å/ä/ö —
    # саме та системна недооцінка, що дала хибний "добрий" Flesch-Kincaid на
    # живому шведському тексті (task-87 §0.2.3). "överraskning" (12 символів,
    # тільки з å/ä/ö) має рахуватись одним довгим словом.
    score = lix("Vilken överraskning.")
    assert score is not None
    words, sentences, long_words = 2, 1, 1
    assert score == round(words / sentences + 100 * long_words / words, 2)


def test_flesch_kincaid_not_used_for_swedish_profile() -> None:
    # Виміряно task-87 §0.2.3: канцелярська шведська проза дає F-K 9.38
    # (усередині англійського банду 7-10 — хибно "чисто"), тоді як LIX того
    # самого тексту — 54.8 (важкий). Профіль sv-SE не має викликати F-K.
    heavy_swedish = (
        "Verksamhetsförlagd utbildning inom ramen för myndighetssamordning "
        "kräver kontinuerlig kompetensutveckling avseende regelefterlevnad."
    )
    item = ContentItem(ref="r", body=heavy_swedish)
    findings_en = check_readability(item, Profile(locale="en-US"))
    findings_sv = check_readability(item, Profile(locale="sv-SE"))
    assert not any(
        f.check == "readability-grade" and "Flesch-Kincaid" in f.detail for f in findings_sv
    )
    # (En-US тут не твердження про правильний вердикт — лише регресійний
    # якір, що en-профіль і далі рахує F-K, не LIX.)
    en_grade_findings = [f for f in findings_en if f.check == "readability-grade"]
    assert all("Flesch-Kincaid" in f.detail for f in en_grade_findings)
