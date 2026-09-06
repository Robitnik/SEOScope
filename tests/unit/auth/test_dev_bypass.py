"""Dev-only доступ без входу (task-73): агентський bearer-токен і
localhost-байпас. Обидва мають лишатись МЕРТВИМИ поза `environment !=
"production"` — це головне, що ловлять ці тести, не сама зручність."""

from __future__ import annotations

from starlette.requests import Request

from app.core.auth.sessions import (
    is_trusted_loopback,
    resolve_agent_token,
    resolve_localhost_bypass,
)
from app.core.config import Settings
from app.schemas.enums import UserRole


def _settings(**overrides: object) -> Settings:
    # `_env_file=None`: ізолює тест від РЕАЛЬНОГО `.env` цього репо — той
    # уже несе `AGENT_DEV_TOKEN`/`AUTH_LOCALHOST_BYPASS` для живої розробки
    # (task-73), і тест «вимкнено за замовчуванням» інакше читав би саме їх.
    return Settings(_env_file=None, **overrides)  # type: ignore[arg-type]


def _request(
    *, headers: dict[str, str] | None = None, client_host: str | None = "127.0.0.1"
) -> Request:
    raw_headers = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    scope: dict[str, object] = {
        "type": "http",
        "method": "GET",
        "path": "/api/v1/dashboard",
        "headers": raw_headers,
        "client": (client_host, 12345) if client_host else None,
    }
    return Request(scope)


# ── agent bearer token ──────────────────────────────────────────────────────


def test_valid_token_authenticates_as_configured_role() -> None:
    settings = _settings(agent_dev_token="secret-token", agent_dev_token_role=UserRole.member)
    request = _request(headers={"Authorization": "Bearer secret-token"})
    auth = resolve_agent_token(request, settings)
    assert auth is not None
    assert auth.role == UserRole.member
    assert auth.origin == "agent"
    assert auth.session_id is None


def test_wrong_token_is_rejected() -> None:
    settings = _settings(agent_dev_token="secret-token")
    request = _request(headers={"Authorization": "Bearer wrong"})
    assert resolve_agent_token(request, settings) is None


def test_missing_header_is_rejected() -> None:
    settings = _settings(agent_dev_token="secret-token")
    assert resolve_agent_token(_request(), settings) is None


def test_disabled_by_default() -> None:
    settings = _settings()  # agent_dev_token=None
    request = _request(headers={"Authorization": "Bearer anything"})
    assert resolve_agent_token(request, settings) is None


def test_dead_in_production_regardless_of_token_value() -> None:
    settings = _settings(agent_dev_token="secret-token", environment="production")
    request = _request(headers={"Authorization": "Bearer secret-token"})
    assert resolve_agent_token(request, settings) is None


# ── localhost bypass ─────────────────────────────────────────────────────────


def test_loopback_without_proxy_headers_is_trusted() -> None:
    assert is_trusted_loopback(_request(client_host="127.0.0.1")) is True
    assert is_trusted_loopback(_request(client_host="::1")) is True


def test_non_loopback_is_never_trusted() -> None:
    assert is_trusted_loopback(_request(client_host="10.0.0.5")) is False
    assert is_trusted_loopback(_request(client_host=None)) is False


def test_a_cloudflare_marker_header_defeats_the_loopback_trust() -> None:
    """Найважливіший тест файлу: `network_mode: host` означає, що тунель,
    форвардячи на `127.0.0.1`, дає ТУ САМУ адресу джерела, що й людина за
    цим-таки браузером. Захищає рівно ЦЕЙ, а не інший, заголовок."""
    for header in (
        "Cf-Connecting-Ip",
        "Cf-Ray",
        "Cf-Visitor",
        "X-Forwarded-For",
        "X-Forwarded-Proto",
        "Forwarded",
    ):
        request = _request(client_host="127.0.0.1", headers={header: "anything"})
        assert is_trusted_loopback(request) is False, f"{header} мав заблокувати довіру"


def test_bypass_disabled_by_default_even_on_trusted_loopback() -> None:
    settings = _settings()  # auth_localhost_bypass=False
    assert resolve_localhost_bypass(_request(), settings) is None


def test_bypass_grants_admin_when_enabled() -> None:
    settings = _settings(auth_localhost_bypass=True)
    auth = resolve_localhost_bypass(_request(), settings)
    assert auth is not None
    assert auth.role == UserRole.admin
    assert auth.origin == "local"
    assert auth.session_id is None


def test_bypass_refuses_tunnel_shaped_requests() -> None:
    settings = _settings(auth_localhost_bypass=True)
    request = _request(client_host="127.0.0.1", headers={"Cf-Connecting-Ip": "8.8.8.8"})
    assert resolve_localhost_bypass(request, settings) is None


def test_bypass_dead_in_production_regardless_of_flag() -> None:
    settings = _settings(auth_localhost_bypass=True, environment="production")
    assert resolve_localhost_bypass(_request(), settings) is None
