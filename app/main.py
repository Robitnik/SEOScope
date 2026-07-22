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
from starlette.staticfiles import StaticFiles

from app import api
from app.api import registered_routers
from app.core.config import get_settings
from app.core.logging import configure_logging, get_logger
from app.core.security import configure_security

logger = get_logger(__name__)


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

    frontend_dir = settings.frontend_dir
    if frontend_dir.is_dir():
        app.mount("/", StaticFiles(directory=frontend_dir, html=True), name="frontend")
    else:
        logger.warning("frontend_dir_missing path=%s", frontend_dir)

    return app


app = create_app()
