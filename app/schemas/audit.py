"""DTO журналу дій (task-72)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel


class AuditEventDTO(BaseModel):
    id: str
    ts: datetime
    actor_kind: str
    actor_id: str | None = None
    actor_email: str | None = None
    actor_name: str | None = None
    actor_role: str | None = None
    ip: str | None = None
    user_agent: str | None = None
    action: str
    resource_type: str | None = None
    resource_id: str | None = None
    resource_label: str | None = None
    method: str | None = None
    path: str | None = None
    status_code: int | None = None
    outcome: str
    duration_ms: int | None = None
    details: dict[str, Any] = {}


class AuditPageDTO(BaseModel):
    """Сторінка стрічки. `next_cursor` — непрозорий для фронту рядок;
    `OFFSET` тут свідомо не використовується (див. репозиторій)."""

    events: list[AuditEventDTO]
    next_cursor: str | None = None


class AuditFacetsDTO(BaseModel):
    actions: list[dict[str, Any]]
    actors: list[dict[str, Any]]
    resource_types: list[str]
