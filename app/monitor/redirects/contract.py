"""Контракт джерела редіректів (task-51 §3.2.1).

Обидва адаптери (`wp_redirection.py`, `probe.py`) повертають один і той самий
`RedirectSourceResult` — решта модуля (diff, severity, майбутній runner) не
знає й не повинен знати, чи правило прийшло з REST плагіна, чи з
blackbox-проби відомих URL.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RedirectRule:
    """Одне правило редіректу, приведене до спільної форми.

    `rule_key` — ідентичність правила в рамках одного джерела: `str(id)`
    плагіна для `wp_redirection`, нормалізований `source_url` для `probe`
    (там власного ID немає). Ключі двох джерел навмисно не перетинаються —
    диф завжди йде в межах одного джерела.
    """

    rule_key: str
    source_url: str
    target_url: str | None
    http_code: int | None
    match_type: str
    regex: bool
    status: str  # "enabled" | "disabled"
    group_name: str | None = None
    title: str | None = None
    external_id: int | None = None


@dataclass(frozen=True)
class RedirectSourceResult:
    """Результат одного збору редіректів клієнта.

    `ok=False` НІКОЛИ не означає «правил немає» (PLAN §12) — лише «не змогли
    перевірити»; `unavailable_reason` каже чому:
    `"auth_failed" | "plugin_missing" | "unavailable" | "unsafe_url"`.
    """

    ok: bool
    source: str  # "redirection" | "probe"
    rules: tuple[RedirectRule, ...] = ()
    raw_snapshot: bytes | None = None
    unavailable_reason: str | None = None
    detail: str | None = None
