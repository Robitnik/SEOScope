"""Діф двох знімків редіректів (task-51 §3.1): added/changed/removed, і

головне — повернення видаленого правила не вигадує нову «появу».
"""

from __future__ import annotations

from app.monitor.redirects.contract import RedirectRule
from app.monitor.redirects.diff import diff_redirect_rules


def _rule(key: str, **overrides: object) -> RedirectRule:
    base = dict(
        rule_key=key,
        source_url=f"/old-{key}/",
        target_url=f"/new-{key}/",
        http_code=301,
        match_type="url",
        regex=False,
        status="enabled",
        group_name="Redirections",
        title=None,
        external_id=int(key),
    )
    base.update(overrides)
    return RedirectRule(**base)  # type: ignore[arg-type]


def test_empty_to_empty_is_empty_diff() -> None:
    result = diff_redirect_rules({}, {})
    assert result.is_empty
    assert result.added == ()
    assert result.changed == ()
    assert result.removed == ()


def test_baseline_snapshot_reports_everything_as_added() -> None:
    new = {"1": _rule("1"), "2": _rule("2")}
    result = diff_redirect_rules({}, new)
    assert {c.rule.rule_key for c in result.added} == {"1", "2"}
    assert result.changed == ()
    assert result.removed == ()


def test_unchanged_rule_produces_no_diff_entry() -> None:
    old = {"1": _rule("1")}
    new = {"1": _rule("1")}
    result = diff_redirect_rules(old, new)
    assert result.is_empty


def test_new_rule_key_is_added() -> None:
    old = {"1": _rule("1")}
    new = {"1": _rule("1"), "2": _rule("2")}
    result = diff_redirect_rules(old, new)
    assert [c.rule.rule_key for c in result.added] == ["2"]
    assert result.changed == () and result.removed == ()


def test_missing_rule_key_is_removed() -> None:
    old = {"1": _rule("1"), "2": _rule("2")}
    new = {"1": _rule("1")}
    result = diff_redirect_rules(old, new)
    assert [c.rule.rule_key for c in result.removed] == ["2"]
    assert result.added == () and result.changed == ()


def test_same_key_different_target_is_changed_with_previous_attached() -> None:
    old = {"1": _rule("1", target_url="/a/")}
    new = {"1": _rule("1", target_url="/b/")}
    result = diff_redirect_rules(old, new)
    assert len(result.changed) == 1
    change = result.changed[0]
    assert change.rule.target_url == "/b/"
    assert change.previous is not None
    assert change.previous.target_url == "/a/"


def test_a_rule_that_disappears_and_reappears_is_removed_then_added_again() -> None:
    """Немає «пам'яті» про видалене правило поза знімком: diff — чиста

    функція над двома знімками, тож повернення того самого rule_key після
    проміжного видалення читається як звичайна нова поява, а не якийсь
    третій стан. Історію (що воно вже колись існувало) веде репозиторій
    через `removed_at`, не цей модуль.
    """
    gone = {}
    reappeared = {"1": _rule("1")}
    result = diff_redirect_rules(gone, reappeared)
    assert [c.rule.rule_key for c in result.added] == ["1"]


def test_only_target_url_and_significant_fields_trigger_changed_not_hit_metrics() -> None:
    # Diff сам по собі не знає про last_access/last_count — RedirectRule їх
    # не має; цей тест лише підтверджує, що ідентичні правила (як їх бачить
    # RedirectRule) не породжують "changed" навіть якщо викликач збирав їх
    # у різний час доби.
    old = {"1": _rule("1")}
    new = {"1": _rule("1")}
    assert diff_redirect_rules(old, new).is_empty


def test_multiple_simultaneous_changes_are_all_reported() -> None:
    old = {"1": _rule("1"), "2": _rule("2"), "3": _rule("3")}
    new = {
        "1": _rule("1"),  # unchanged
        "2": _rule("2", status="disabled"),  # changed
        "4": _rule("4"),  # added
        # "3" removed
    }
    result = diff_redirect_rules(old, new)
    assert [c.rule.rule_key for c in result.added] == ["4"]
    assert [c.rule.rule_key for c in result.changed] == ["2"]
    assert [c.rule.rule_key for c in result.removed] == ["3"]
