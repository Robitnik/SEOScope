"""Фінгерпринт правила редіректу (task-51 §3.1/§1.3) — єдине джерело істини

для «це та сама поведінка чи інша». `last_access`/`last_count`/`position` тут
НАВМИСНО відсутні: це метрики спрацювання/сортування, не поведінки сайту.
Redirection пише `last_access` при кожному хіті відвідувача — без цього
виключення стрічка змін щогодини рясніла б фальшивим «правило змінилося» на
кожен реальний перехід по посиланню, а не на реальну зміну правила.
"""

from __future__ import annotations

import hashlib

from app.monitor.redirects.contract import RedirectRule

_SEP = "\x1f"


def rule_fingerprint(rule: RedirectRule) -> str:
    """sha256 по значущих полях правила — стабільний за `last_access`/

    `last_count`/`position`. Порядок полів фіксований (не полінь на майбутнє
    поле міняти порядок — це змінить усі наявні фінгерпринти й підніме
    хибний «все змінилося» на першому ж прогоні після апдейту).
    """
    parts = (
        rule.source_url,
        rule.match_type,
        "1" if rule.regex else "0",
        rule.target_url or "",
        str(rule.http_code) if rule.http_code is not None else "",
        rule.status,
        rule.group_name or "",
        rule.title or "",
    )
    return hashlib.sha256(_SEP.join(parts).encode("utf-8")).hexdigest()
