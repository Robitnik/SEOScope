"""Діф двох знімків списку редіректів (task-51 §3.1) — чиста функція:

приймає попередній і новий набір правил за `rule_key`, повертає
added/changed/removed. Жодного звернення до БД чи мережі — той самий підхід,
що `app/tools/redirects/input.py` (нормалізація без побічних ефектів), щоб
усі форми змін покривались юніт-тестами без фікстур.

Класифікація серйозності, яка потребує контексту поза цим знімком (чи ціль
офсайт, чи джерело — відомий живий URL клієнта), навмисно лишається за
викликачем (`app/monitor/redirects/severity.py` для першого, майбутній
`runner.py` для другого — там є доступ до `MonitorPage`) — тут тільки факт
зміни.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.monitor.redirects.contract import RedirectRule
from app.monitor.redirects.fingerprint import rule_fingerprint


@dataclass(frozen=True)
class RuleChange:
    kind: str  # "added" | "removed" | "changed"
    rule: RedirectRule
    previous: RedirectRule | None = None


@dataclass(frozen=True)
class RedirectSetDiff:
    added: tuple[RuleChange, ...] = ()
    changed: tuple[RuleChange, ...] = ()
    removed: tuple[RuleChange, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not (self.added or self.changed or self.removed)


def diff_redirect_rules(
    old_rules: dict[str, RedirectRule], new_rules: dict[str, RedirectRule]
) -> RedirectSetDiff:
    """`old_rules`/`new_rules` — `rule_key -> RedirectRule` знімки одного й

    того самого джерела (§3.1: ключі різних джерел не перетинаються, тож
    змішувати джерела в одному виклику немає сенсу — це зона відповідальності
    репозиторію, який тримає їх окремо через `(client_id, source, rule_key)`).
    """
    added: list[RuleChange] = []
    changed: list[RuleChange] = []
    removed: list[RuleChange] = []

    for key, new_rule in new_rules.items():
        old_rule = old_rules.get(key)
        if old_rule is None:
            added.append(RuleChange("added", new_rule))
        elif rule_fingerprint(old_rule) != rule_fingerprint(new_rule):
            changed.append(RuleChange("changed", new_rule, previous=old_rule))

    for key, old_rule in old_rules.items():
        if key not in new_rules:
            removed.append(RuleChange("removed", old_rule))

    return RedirectSetDiff(added=tuple(added), changed=tuple(changed), removed=tuple(removed))
