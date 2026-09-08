"""Locale gating (task-87 §1.2): кожен рядок мапи лейнів — власний тест.

`Profile(locale="en-US")` (дефолт) має лишатись байт-у-байт таким, яким був
до task-87 — це регресійний якір; кожен `sv-SE`-тест перевіряє або нову
поведінку, або що англійський лейн для шведської мовчить.
"""

from __future__ import annotations

from app.contentqa import engine
from app.contentqa.model import ContentDoc, ContentItem
from app.contentqa.policy_rules import check_meta, check_slop_and_voice_policy
from app.contentqa.rules import Profile, check_brand_voice_own, check_wikipedia_slop


def test_locale_rules_adjusted_absent_for_english() -> None:
    item = ContentItem(ref="r", title="Short Title")
    doc = ContentDoc(source="s.xlsx", doc_type="strategy_xlsx", items=(item,))
    findings = engine.run_checks(doc, Profile(locale="en-US"))
    assert not any(f.check == "locale-rules-adjusted" for f in findings)


def test_locale_rules_adjusted_present_exactly_once_for_swedish() -> None:
    item = ContentItem(ref="r", title="Kort titel")
    doc = ContentDoc(source="s.xlsx", doc_type="strategy_xlsx", items=(item,))
    findings = engine.run_checks(doc, Profile(locale="sv-SE"))
    hits = [f for f in findings if f.check == "locale-rules-adjusted"]
    assert len(hits) == 1
    assert hits[0].severity == "info"


def test_en_dash_not_flagged_for_swedish_but_em_dash_still_is() -> None:
    # en dash — коректний шведський tankstreck (task-87 §0.2.1); em dash
    # лишається домашнім правилом для будь-якої мови.
    en_dash_item = ContentItem(ref="r", description="Öppet 10–18 varje dag.")
    em_dash_item = ContentItem(ref="r", description="Snabbt — pålitligt — tryggt.")
    sv = Profile(locale="sv-SE")
    en_dash_findings = check_slop_and_voice_policy(en_dash_item, sv)
    em_dash_findings = check_slop_and_voice_policy(em_dash_item, sv)
    assert not any(f.check == "voice-em-dash" for f in en_dash_findings)
    assert any(f.check == "voice-em-dash" for f in em_dash_findings)


def test_us_gb_spelling_lane_disabled_for_swedish() -> None:
    # "program" — звичайне шведське слово; для en-US воно ловилось би як
    # "programme"-мисматч (task-87 §0.2.2).
    item = ContentItem(ref="r", description="Vårt program för spökjakt börjar i kväll.")
    findings = check_brand_voice_own(item, Profile(locale="sv-SE"))
    assert not any(f.check == "voice-locale-mismatch" for f in findings)


def test_swedish_banned_opener_fires() -> None:
    item = ContentItem(ref="r", description="Köp spökutrustning hos LaxTon idag.")
    findings = check_meta(item, Profile(locale="sv-SE"))
    assert any(f.check == "meta-desc-banned-opener" for f in findings)


def test_swedish_filler_term_fires() -> None:
    item = ContentItem(ref="r", description="Vår banbrytande teknik ger perfekt resultat.")
    findings = check_slop_and_voice_policy(item, Profile(locale="sv-SE"))
    assert any(f.check == "slop-filler-term" for f in findings)


def test_swedish_ai_slop_phrase_fires() -> None:
    item = ContentItem(ref="r", description="Oavsett om du letar efter spöken eller inte.")
    findings = check_slop_and_voice_policy(item, Profile(locale="sv-SE"))
    assert any(f.check == "slop-phrase" for f in findings)


def test_swedish_word_swap_nudge() -> None:
    item = ContentItem(ref="r", description="Vi nyttjar modern utrustning i syfte att hjälpa dig.")
    findings = check_brand_voice_own(item, Profile(locale="sv-SE"))
    swaps = [f for f in findings if f.check == "voice-word-swap"]
    assert any("nyttja" in f.detail for f in swaps)


def test_swedish_colloquial_contraction_flagged() -> None:
    item = ContentItem(ref="r", description="Vi ska dom prata med sen.")
    findings = check_brand_voice_own(item, Profile(kind="page", locale="sv-SE"))
    assert any(f.check == "voice-contraction-in-formal" for f in findings)


def test_swedish_money_leak_with_kr_currency() -> None:
    item = ContentItem(ref="r", description="Vår avgift är 500 kr per timme.")
    findings = check_brand_voice_own(item, Profile(locale="sv-SE"))
    assert any(f.check == "voice-money-leak" for f in findings)


def test_keyword_in_title_stem_match_catches_swedish_inflection() -> None:
    # Виміряно task-87 §0.2.4: точний токен-збіг ("mössor" vs "Mössa …") дає
    # хибний meta-title-keyword-missing на живих даних laxton.se.
    item = ContentItem(
        ref="r", title="LaxTon Mössa 1 med Siluett Mörkgrå", keywords=("mössor",),
    )
    findings = check_meta(item, Profile(locale="sv-SE"))
    assert not any(f.check == "meta-title-keyword-missing" for f in findings)


def test_keyword_in_title_stem_match_still_flags_a_real_miss() -> None:
    item = ContentItem(ref="r", title="LaxTon Presentkort", keywords=("spökjakt",))
    findings = check_meta(item, Profile(locale="sv-SE"))
    assert any(f.check == "meta-title-keyword-missing" for f in findings)


def test_swedish_anchor_generic_extends_the_english_set() -> None:
    item = ContentItem(ref="r", intent="commercial", internal_links=("läs mer (/produkter/)",))
    from app.contentqa.rules import check_links

    findings = check_links(item, Profile(locale="sv-SE"))
    assert any(f.check == "anchor-generic" for f in findings)


def test_swedish_wikipedia_slop_equivalents_fire() -> None:
    item = ContentItem(
        ref="r",
        description="Enligt vissa experter är detta inte bara populärt utan också effektivt.",
    )
    sv = Profile(locale="sv-SE")
    findings = check_wikipedia_slop(item, sv)
    checks = {f.check for f in findings}
    assert "slop-vague-attribution" in checks
    assert "slop-negative-parallelism" in checks


def test_english_only_wikipedia_slop_lanes_silent_for_swedish() -> None:
    # participle-chain/undue-significance/rule-of-three — англійський синтаксис,
    # без шведського відповідника (task-87 §1.2) — не мають хибно спрацьовувати
    # на нейтральному шведському тексті.
    item = ContentItem(ref="r", description="Detta är en vanlig produktbeskrivning utan problem.")
    findings = check_wikipedia_slop(item, Profile(locale="sv-SE"))
    checks = {f.check for f in findings}
    assert "slop-participle-chain" not in checks
    assert "slop-undue-significance" not in checks


def test_process_leak_and_tool_failure_stay_active_for_swedish() -> None:
    # Мовно-нейтральні: назви наших інструментів/артефактів леакають у
    # документ будь-якою мовою (task-87 §1.2).
    item = ContentItem(ref="r", description="Data hämtad från vår Ahrefs-session för kunden.")
    findings = check_slop_and_voice_policy(item, Profile(locale="sv-SE"))
    assert any(f.check == "process-leak" for f in findings)
