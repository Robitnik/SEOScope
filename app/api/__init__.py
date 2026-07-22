"""API router registry.

Feature routers register themselves here (typically via a module-level
``register_router(router)`` call in their own file) so that ``app/main.py``
never has to be edited when a new router module is added under ``app/api/``.
"""

from __future__ import annotations

from fastapi import APIRouter

_routers: list[APIRouter] = []


def register_router(router: APIRouter) -> None:
    _routers.append(router)


def registered_routers() -> list[APIRouter]:
    return list(_routers)
