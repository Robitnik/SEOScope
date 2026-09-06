"""Редакція секретів перед записом у журнал (task-72).

Інваріант PLAN §13 («секрети не потрапляють у БД, events, логи») сильніший за
повноту журналу: якщо вибір між «зберегти тіло запиту як є» і «не зберегти
пароль» — виграє друге.

Список ключів береться з `RedactionFilter` (`app/core/logging.py`), а не
дублюється: інакше додавання нового секретного поля довелось би пам'ятати
зробити двічі, і одного разу його забудуть.
"""

from __future__ import annotations

from typing import Any

from app.core.logging import REDACTED, _redaction_filter

# Скільки байтів JSON-зрізу тіла ми готові тримати на рядок. Журнал має
# відповідати «що людина зробила», а не бути копією трафіку: 4 КБ вистачає
# на будь-який наш реальний запит, а імпорт на 2 МБ не має роздувати таблицю.
MAX_DETAILS_BYTES = 4096


def _keys() -> set[str]:
    return _redaction_filter.redact_keys


def redact(value: Any, *, depth: int = 0) -> Any:
    """Рекурсивно замінює значення секретних ключів на маску."""
    if depth > 6:
        return "<nested>"
    if isinstance(value, dict):
        keys = _keys()
        return {
            str(k): (REDACTED if str(k).lower() in keys else redact(v, depth=depth + 1))
            for k, v in value.items()
        }
    if isinstance(value, list | tuple):
        return [redact(item, depth=depth + 1) for item in value[:50]]
    return value


def clamp(details: dict[str, Any]) -> dict[str, Any]:
    """Обрізає надто велике тіло, лишаючи слід про сам факт обрізання."""
    import json

    try:
        encoded = json.dumps(details, default=str)
    except (TypeError, ValueError):
        return {"unserializable": True}
    if len(encoded) <= MAX_DETAILS_BYTES:
        return details
    return {
        "truncated": True,
        "original_bytes": len(encoded),
        "preview": encoded[:MAX_DETAILS_BYTES],
    }


def prepare(details: dict[str, Any] | None) -> dict[str, Any]:
    if not details:
        return {}
    redacted = redact(details)
    assert isinstance(redacted, dict)
    return clamp(redacted)
