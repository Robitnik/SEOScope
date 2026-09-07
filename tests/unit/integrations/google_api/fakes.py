"""Shared fakes for the direct Google API transport tests (task-83 §10).

Everything runs on `httpx.MockTransport` — no network — so the real request
building and response parsing are exercised, only the socket is faked.
"""

from __future__ import annotations

import httpx

from app.integrations.google_api.auth import GoogleApiSession, GoogleAuth

TOKEN_URL_PART = "oauth2.googleapis.com/token"

Responder = httpx.Response | list[httpx.Response] | object


class FakeGoogle:
    """A programmable `httpx` handler.

    `on(substr, responder)` routes any request whose URL contains `substr`;
    `responder` is an `httpx.Response`, a `list` of them (consumed in order,
    last repeats), or a `callable(request) -> httpx.Response`. The token
    endpoint answers `200 {access_token, expires_in}` unless routed.
    """

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self._routes: list[tuple[str, Responder]] = []
        self.refresh_count = 0

    def on(self, substr: str, responder: Responder) -> FakeGoogle:
        self._routes.append((substr, responder))
        return self

    def _dispatch(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url)
        is_token = TOKEN_URL_PART in url
        for substr, responder in self._routes:
            # A broad route ("googleapis.com") must not hijack the token
            # endpoint — only an explicitly token-scoped route may.
            if substr in url and (not is_token or ("token" in substr or "oauth2" in substr)):
                return self._resolve(responder, request)
        if is_token:
            self.refresh_count += 1
            return httpx.Response(200, json={"access_token": "access-xyz", "expires_in": 3599})
        return httpx.Response(404, json={"error": {"status": "NOT_FOUND", "message": "unrouted"}})

    def _resolve(self, responder: Responder, request: httpx.Request) -> httpx.Response:
        if isinstance(responder, list):
            item = responder.pop(0) if len(responder) > 1 else responder[0]
            return self._resolve(item, request)
        if isinstance(responder, httpx.Response):
            return responder
        assert callable(responder)
        result = responder(request)
        assert isinstance(result, httpx.Response)
        return result

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._dispatch)

    def auth(self, **overrides: object) -> GoogleAuth:
        kwargs: dict[str, object] = {
            "client_id": "cid",
            "client_secret": "secret-value-shh",
            "refresh_token": "refresh-value-shh",
            "timeout_seconds": 5.0,
        }
        kwargs.update(overrides)
        return GoogleAuth(transport=self.transport, **kwargs)  # type: ignore[arg-type]

    def session(self, auth: GoogleAuth | None = None) -> GoogleApiSession:
        return GoogleApiSession(auth or self.auth(), transport=self.transport, timeout_seconds=5.0)


def collect_strings(obj: object) -> list[str]:
    """Every string anywhere in a nested structure — used to assert no secret
    leaked into a warning / provenance / error text (task-83 §10.1)."""
    out: list[str] = []
    if isinstance(obj, str):
        out.append(obj)
    elif isinstance(obj, dict):
        for key, value in obj.items():
            out.extend(collect_strings(key))
            out.extend(collect_strings(value))
    elif isinstance(obj, list | tuple | set):
        for item in obj:
            out.extend(collect_strings(item))
    return out
