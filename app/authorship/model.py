"""Хто створив рядок (task-93 план §2, рішення D2).

Атрибуція живе як атрибут самого рядка (`created_by_user_id` +
`created_by_kind` на `report_runs`/`report_batches`/`content_qa_packages`),
а не похідне з `audit_events` — журнал для створювальних POST-маршрутів
пише `resource_id=NULL` (id ще не існує в момент запису дії), має ретенцію
365 днів і чергу, яка може відкидати рядки при переповненні. Див. план §1.2.

`kind` — той самий словник, що й `AuditRecord.actor_kind`
(`app/audit/middleware.py`): `user | agent | local | cli | system`, і додатково
`None` = невідомо (рядок, створений до відстеження авторства, або
неоднозначний бекфіл). `None` НІКОЛИ не підміняється на `"system"` — це два
різні факти («працював фоновий процес» проти «не знаємо, хто»).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.api.deps import Actor

# Ідентифікатори, які `Actor.id`/`AuthenticatedSession.user_id` несе для
# невходжених способів автентифікації (task-73/task-71) — не UUID, тож
# `user_id` для них завжди `None`, а не невалідний FK.
_NON_USER_IDS = frozenset({"agent", "local", "cli", "system"})


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
    except (ValueError, AttributeError):
        return False
    return True


@dataclass(frozen=True)
class Attribution:
    """`kind=None` означає «невідомо» — записується як `NULL` в обидві
    колонки, ніколи не вгадується."""

    kind: str | None
    user_id: str | None

    @classmethod
    def from_actor(cls, actor: Actor) -> Attribution:
        """Чиста функція без I/O (план §2 D2): `kind` береться з
        `Actor.kind`, `user_id` — лише коли це справжній акаунт людини
        (звичайний логін, особистий токен task-79, MCP OAuth task-78 —
        усі кладуть реальний GUID у `Actor.id`). Dev bearer-токен
        (`id="agent"`), localhost-байпас (`id="local"`), CLI і system
        ніколи не мають рядка в `users`, тож `user_id` лишається `None`.
        """
        if actor.kind in _NON_USER_IDS and actor.id in _NON_USER_IDS:
            return cls(kind=actor.kind, user_id=None)
        if _is_uuid(actor.id):
            return cls(kind=actor.kind, user_id=actor.id)
        # Оборонний випадок: невідомий `Actor.kind`/`id`, що не UUID і не
        # входить у відомий невходжений набір — краще `unknown`, ніж хибний FK.
        return cls(kind=None, user_id=None)

    @classmethod
    def cli(cls) -> Attribution:
        return cls(kind="cli", user_id=None)

    @classmethod
    def unknown(cls) -> Attribution:
        return cls(kind=None, user_id=None)
