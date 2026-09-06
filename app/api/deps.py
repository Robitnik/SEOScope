"""Shared FastAPI dependencies for `app/api/*` routers (PLAN §10/§13).

Kept deliberately thin: routers do the actual work via repositories bridged
through `AsyncSession.run_sync` (see `app/db/repositories/__init__.py`), this
module only wires the request-scoped bits every router needs so they don't
each reinvent them.

Every dependency is exposed as an `Annotated[...]` alias (`SessionDep`,
`PaginationDep`, ...) rather than a `= Depends(...)` default value: ruff's
`B008` flags a bare call in a default value, and `Annotated` is also the
style FastAPI itself recommends since 0.95.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, HTTPException, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth.policy import role_satisfies
from app.core.config import Settings, get_settings
from app.db.session import get_db
from app.schemas.enums import UserRole

__all__ = [
    "Actor",
    "ActorDep",
    "AdminActorDep",
    "Pagination",
    "PaginationDep",
    "SessionDep",
    "SettingsDep",
    "current_actor",
    "get_session",
    "pagination",
    "require_role",
]


async def get_session() -> AsyncIterator[AsyncSession]:
    async for session in get_db():
        yield session


SessionDep = Annotated[AsyncSession, Depends(get_session)]


def get_settings_dep() -> Settings:
    return get_settings()


SettingsDep = Annotated[Settings, Depends(get_settings_dep)]


@dataclass(frozen=True)
class Pagination:
    limit: int
    offset: int


def pagination(
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Pagination:
    return Pagination(limit=limit, offset=offset)


PaginationDep = Annotated[Pagination, Depends(pagination)]


@dataclass(frozen=True)
class Actor:
    """Хто виконує дію (task-71).

    Той самий seam, що стояв тут заглушкою з MVP, — тепер із реальним
    користувачем із сесії. Фонові процеси (worker, CLI, планувальники) HTTP
    не ходять і користуються `Actor.system()` / `Actor.cli()`; на цю ж
    структуру спиратиметься журнал дій (task-72).
    """

    id: str = "system"
    kind: str = "system"
    email: str | None = None
    name: str | None = None
    role: UserRole | None = None
    session_id: str | None = None
    ip: str | None = None

    @property
    def is_authenticated(self) -> bool:
        return self.kind == "user"

    @classmethod
    def system(cls) -> Actor:
        return cls()

    @classmethod
    def cli(cls) -> Actor:
        return cls(id="cli", kind="cli")


def current_actor(request: Request) -> Actor:
    """Читає те, що вже поклав `AuthMiddleware` — повторного запиту в БД тут
    немає навмисно: резолв сесії коштує SELECT, і робити його двічі на запит
    (у middleware і в залежності) було б чистим податком."""
    auth = getattr(request.state, "auth_session", None)
    if auth is None:
        # `auth_enabled=False` (локальна розробка) або публічний шлях.
        return Actor.system()
    client_host = request.client.host if request.client else None
    # `origin` (task-73): "cookie" — звичайний людський логін ("user");
    # "agent"/"local" переносяться як є — журнал дій (task-72) одразу
    # показує, що це dev bearer-токен чи localhost-байпас, а не хтось, хто
    # заходив формою входу.
    kind = "user" if auth.origin == "cookie" else auth.origin
    return Actor(
        id=auth.user_id,
        kind=kind,
        email=auth.email,
        name=auth.name,
        role=auth.role,
        session_id=auth.session_id,
        ip=client_host,
    )


ActorDep = Annotated[Actor, Depends(current_actor)]


def require_role(minimum: UserRole) -> Callable[[Actor], Actor]:
    """Точкова перевірка ролі для роутера.

    Дублює те, що вже робить `AuthMiddleware` за шляхом, — і це свідомо:
    middleware ловить усе за префіксом, а ця залежність робить вимогу
    видимою в сигнатурі ендпоінта й у згенерованій OpenAPI-схемі. При
    `auth_enabled=False` (тести/локальна розробка) пропускає.
    """

    def _dependency(actor: ActorDep) -> Actor:
        if not get_settings().auth_enabled:
            return actor
        if actor.role is None or not role_satisfies(actor.role, minimum):
            raise HTTPException(
                status_code=403,
                detail={
                    "error_code": "forbidden",
                    "message": "Your role does not allow this action.",
                },
            )
        return actor

    return _dependency


AdminActorDep = Annotated[Actor, Depends(require_role(UserRole.admin))]
