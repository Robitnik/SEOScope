"""Security headers, restrictive CORS, and a path-traversal guard for artifacts."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

from app.core.config import Settings

_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": "default-src 'self'",
    "Permissions-Policy": "geolocation=(), microphone=(), camera=()",
}


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        response = await call_next(request)
        for header, value in _SECURITY_HEADERS.items():
            response.headers.setdefault(header, value)
        return response


def configure_security(app: FastAPI, settings: Settings) -> None:
    """Attach restrictive, same-origin-by-default CORS and security headers."""
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_allow_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        allow_headers=["*"],
    )


class PathTraversalError(ValueError):
    """Raised when a requested path resolves outside its allowed root."""


def safe_resolve(root: Path, relative_path: str) -> Path:
    """Resolve ``relative_path`` under ``root``, refusing traversal/absolute escapes.

    Used by artifact download endpoints to guarantee the resolved path stays
    inside the configured artifacts root.
    """
    root_resolved = root.resolve()
    candidate = Path(relative_path)
    if candidate.is_absolute():
        raise PathTraversalError(f"Absolute paths are not allowed: {relative_path!r}")

    resolved = (root_resolved / candidate).resolve()
    if resolved != root_resolved and root_resolved not in resolved.parents:
        raise PathTraversalError(f"Path escapes root {root_resolved}: {relative_path!r}")
    return resolved
