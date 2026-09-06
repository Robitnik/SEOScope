"""FastAPI application factory.

Other tasks add functionality by dropping a module under ``app/api/`` that
defines an ``APIRouter`` and calls ``register_router(router)`` at import
time — this file discovers and includes it automatically. Do not edit this
file to wire up a new router.
"""

from __future__ import annotations

import importlib
import pkgutil

from fastapi import FastAPI
from starlette.responses import Response
from starlette.staticfiles import StaticFiles
from starlette.types import Scope

from app import api
from app.api import registered_routers
from app.core.config import get_settings
from app.core.logging import configure_logging, get_logger
from app.core.security import configure_security
from app.mcp.asgi import attach_mcp

logger = get_logger(__name__)


class RevalidatingStaticFiles(StaticFiles):
    """Prevent the browser from pinning an obsolete ES-module graph.

    StaticFiles provides ETags/Last-Modified but no Cache-Control policy. A
    browser can therefore heuristically reuse an old JS module without even
    asking the server whether it changed. Revalidation keeps 304s for unchanged
    files while guaranteeing that a changed HTML/JS/CSS file is fetched.
    """

    async def get_response(self, path: str, scope: Scope) -> Response:
        response = await super().get_response(path, scope)
        is_frontend_code = path in {"", ".", "index.html"} or path.lower().endswith(
            (".html", ".js", ".css")
        )
        if is_frontend_code or response.media_type in {
            "text/html",
            "text/css",
            "text/javascript",
            "application/javascript",
        }:
            response.headers["Cache-Control"] = "no-cache, max-age=0, must-revalidate"
        return response


def _discover_routers() -> None:
    """Import every submodule under app.api so its register_router() call runs."""
    for module_info in pkgutil.iter_modules(api.__path__, prefix=f"{api.__name__}."):
        importlib.import_module(module_info.name)


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)

    app = FastAPI(title=settings.app_name)
    configure_security(app, settings)

    _discover_routers()
    for router in registered_routers():
        app.include_router(router)

    # ДО статики: `mount("/")` нижче ковтає будь-який шлях, тож маршрути
    # MCP і OAuth мають бути зареєстровані раніше.
    attach_mcp(app, settings)

    frontend_dir = settings.frontend_dir
    if frontend_dir.is_dir():
        app.mount("/", RevalidatingStaticFiles(directory=frontend_dir, html=True), name="frontend")
    else:
        logger.warning("frontend_dir_missing path=%s", frontend_dir)

    return app


app = create_app()
