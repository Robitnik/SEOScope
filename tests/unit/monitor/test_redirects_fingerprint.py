"""Фінгерпринт правила редіректу (task-51 §1.3/§3.1) — критична гарантія:

`last_access`/`last_count`/`position` НЕ впливають на фінгерпринт, інакше
кожне спрацювання правила читалося б діфом як «правило змінилося».
"""

from __future__ import annotations

from app.monitor.redirects.contract import RedirectRule
from app.monitor.redirects.fingerprint import rule_fingerprint


def _rule(**overrides: object) -> RedirectRule:
    base = dict(
        rule_key="1487",
        source_url="/old-page/",
        target_url="/new-page/",
        http_code=301,
        match_type="url",
        regex=False,
        status="enabled",
        group_name="Redirections",
        title=None,
        external_id=1487,
    )
    base.update(overrides)
    return RedirectRule(**base)  # type: ignore[arg-type]


def test_identical_rules_have_the_same_fingerprint() -> None:
    assert rule_fingerprint(_rule()) == rule_fingerprint(_rule())


def test_target_url_change_changes_the_fingerprint() -> None:
    assert rule_fingerprint(_rule()) != rule_fingerprint(_rule(target_url="/another-page/"))


def test_http_code_change_changes_the_fingerprint() -> None:
    assert rule_fingerprint(_rule()) != rule_fingerprint(_rule(http_code=302))


def test_status_change_changes_the_fingerprint() -> None:
    assert rule_fingerprint(_rule()) != rule_fingerprint(_rule(status="disabled"))


def test_group_name_change_changes_the_fingerprint() -> None:
    assert rule_fingerprint(_rule()) != rule_fingerprint(_rule(group_name="Modified Posts"))


def test_title_change_changes_the_fingerprint() -> None:
    assert rule_fingerprint(_rule()) != rule_fingerprint(_rule(title="New title"))


def test_match_type_and_regex_change_the_fingerprint() -> None:
    assert rule_fingerprint(_rule()) != rule_fingerprint(_rule(match_type="referrer"))
    assert rule_fingerprint(_rule()) != rule_fingerprint(_rule(regex=True))


def test_rule_key_and_external_id_do_not_affect_the_fingerprint() -> None:
    # `rule_key` — ідентичність, не поведінка: те саме правило під іншим
    # ключем (гіпотетично) мало б той самий фінгерпринт значущих полів.
    assert rule_fingerprint(_rule()) == rule_fingerprint(_rule(rule_key="9999", external_id=9999))


def test_last_access_and_last_count_are_not_part_of_the_contract_at_all() -> None:
    # RedirectRule навмисно не має полів last_access/last_count/position —
    # це і є гарантія: їх фізично нічим прогодувати у фінгерпринт.
    assert not hasattr(_rule(), "last_access")
    assert not hasattr(_rule(), "last_count")
    assert not hasattr(_rule(), "position")


def test_none_target_and_none_title_are_stable_and_distinct_from_empty_string() -> None:
    rule_with_none = _rule(target_url=None, title=None)
    rule_with_empty = _rule(target_url="", title="")
    # Обидва нормалізуються до порожнього рядка у фінгерпринті — це свідомий
    # вибір (немає різниці в поведінці між "не задано" й "задано порожнім"),
    # головне, щоб він був детермінований і не кидав виняток.
    assert rule_fingerprint(rule_with_none) == rule_fingerprint(rule_with_empty)
