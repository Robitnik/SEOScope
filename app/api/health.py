"""Liveness/readiness endpoints."""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.api import register_router
from app.db.health import database_ready

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/audit-healthz")
async def audit_health() -> dict[str, object]:
    """Стан черги журналу (task-72).

    Скинуті рядки — видима деградація, а не мовчазна втрата: якщо `dropped`
    росте, журнал не встигає, і це помітно тут, а не за півроку в судовому
    сенсі «а чому цієї дії немає».
    """
    from app.audit.sink import get_audit_sink
    from app.core.config import get_settings

    settings = get_settings()
    if not settings.audit_enabled:
        return {"enabled": False}
    return {"enabled": True, **get_audit_sink().health()}


@router.get("/readyz")
async def readyz() -> JSONResponse:
    ready, detail = await database_ready()
    return JSONResponse(
        status_code=200 if ready else 503,
        content={"status": "ok" if ready else "not_ready", "checks": {"database": detail}},
    )


register_router(router)
