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

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.db.session import get_db

__all__ = [
    "Actor",
    "ActorDep",
    "Pagination",
    "PaginationDep",
    "SessionDep",
    "SettingsDep",
    "current_actor",
    "get_session",
    "pagination",
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
    """Placeholder for the authenticated caller (PLAN §13).

    MVP deployment is behind an auth reverse proxy on a private network, so
    there is no app-level login yet; this stub keeps a single seam routers
    can depend on (`current_actor`) so wiring real auth later touches this
    function, not every call site.
    """

    id: str = "system"


def current_actor() -> Actor:
    return Actor()


ActorDep = Annotated[Actor, Depends(current_actor)]
