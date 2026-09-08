"""Реєстр лексиконів + шведські списки (task-87 §1.1-1.2)."""

from __future__ import annotations

from app.contentqa.lexicon import get_lexicon
from app.contentqa.lexicon.sv import LEXICON as SV


def test_registry_returns_none_for_english() -> None:
    # "en" ходить через policy_bridge/AISeoTools, не через реєстр.
    assert get_lexicon("en") is None


def test_registry_returns_none_for_unknown_language() -> None:
    assert get_lexicon("ru") is None


def test_registry_returns_swedish_lexicon() -> None:
    assert get_lexicon("sv") is SV


def test_swedish_banned_openers_do_not_fire_on_english_words() -> None:
    assert "explore" not in SV.banned_openers
    assert "köp" in SV.banned_openers


def test_swedish_ai_slop_phrases_are_swedish_not_translated_english() -> None:
    assert "look no further" not in SV.ai_slop_phrases
    assert any("oavsett" in p for p in SV.ai_slop_phrases)


def test_swedish_report_en_dash_is_disabled() -> None:
    # tankstreck — коректна шведська пунктуація (task-87 §0.2.1).
    assert SV.report_en_dash is False


def test_swedish_readability_band_is_lix_not_flesch_kincaid() -> None:
    assert SV.readability_min == 30.0
    assert SV.readability_max == 45.0


def test_swedish_disabled_lanes_documents_the_gap() -> None:
    assert SV.disabled_lanes  # непорожньо: PLAN §12, не мовчазна "чистота"
