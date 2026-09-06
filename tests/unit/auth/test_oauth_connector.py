"""OAuth-сервер remote-конектора (task-78).

Перевіряємо не «чи SDK вміє OAuth» (це його робота й його тести), а рівно
ті інваріанти, які додає ЦЕЙ проєкт: одноразовість коду, ротація refresh,
відсутність секретів клієнта, і — головне — що виданий токен доступу це
звичайний рядок `agent_tokens`, який живе за правилами task-79: стеля
`member`, права звужуються до поточної ролі власника, деактивація людини
вбиває конектор негайно.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from mcp.shared.auth import OAuthClientInformationFull

from app.core.auth.sessions import resolve_personal_token_sync
from app.core.config import Settings
from app.db.base import utcnow
from app.db.models import AgentToken
from app.db.repositories import oauth as repo
from app.db.repositories.users import UserRepository
from app.mcp.oauth import ACCESS_TOKEN_DAYS, SCOPE, _issue_access_token
from app.schemas.enums import UserRole

REDIRECT = "https://claude.ai/api/mcp/auth_callback"


def _client(client_id: str = "cid-1") -> OAuthClientInformationFull:
    return OAuthClientInformationFull(
        client_id=client_id,
        client_name="Claude",
        redirect_uris=[REDIRECT],  # type: ignore[list-item]
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        token_endpoint_auth_method="none",
        scope=SCOPE,
    )


def _settings(**overrides: object) -> Settings:
    return Settings(**overrides)  # type: ignore[arg-type]


def _user(db, email="oauth-user@example.com", role=UserRole.member):
    user = UserRepository(db).create(email=email, name="OAuth User", role=role, password="pw-123")
    db.flush()
    return user


def _pending(db, client_id="cid-1"):
    return repo.create_pending(
        db,
        client_id=client_id,
        scopes=[SCOPE],
        code_challenge="challenge-value",
        redirect_uri=REDIRECT,
        redirect_uri_provided_explicitly=True,
        state="state-1",
        resource="https://seo.theyovth.space/mcp",
    )


def test_registered_client_never_keeps_a_secret(db):
    payload = _client().model_dump(mode="json", exclude_none=True)
    repo.save_client(db, "cid-1", payload)
    stored = repo.load_client(db, "cid-1")
    assert stored is not None
    assert "client_secret" not in stored


def test_authorization_code_works_exactly_once(db):
    user = _user(db)
    pending = _pending(db)
    code = repo.issue_auth_code(db, pending=pending, user_id=user.id)

    assert repo.get_auth_code(db, code) is not None
    repo.consume_auth_code(db, repo.get_auth_code(db, code))
    assert repo.get_auth_code(db, code) is None, "спожитий код не має воскресати"


def test_pending_request_cannot_be_approved_twice(db):
    user = _user(db)
    pending = _pending(db)
    repo.issue_auth_code(db, pending=pending, user_id=user.id)
    assert repo.get_pending(db, pending.id) is None


def test_pending_request_expires(db):
    pending = _pending(db)
    later = utcnow() + repo.PENDING_TTL + timedelta(seconds=1)
    assert repo.get_pending(db, pending.id, now=later) is None


def test_code_is_never_stored_in_plaintext(db):
    user = _user(db)
    pending = _pending(db)
    code = repo.issue_auth_code(db, pending=pending, user_id=user.id)
    row = repo.get_auth_code(db, code)
    assert row is not None
    assert code not in row.code_sha256
    assert len(row.code_sha256) == 64


def test_revoked_refresh_token_stops_working(db):
    user = _user(db)
    raw = repo.issue_refresh_token(db, client_id="cid-1", user_id=user.id, scopes=SCOPE)
    row = repo.get_refresh_token(db, raw)
    assert row is not None
    repo.revoke_refresh_token(db, row)
    assert repo.get_refresh_token(db, raw) is None


def test_access_token_is_an_ordinary_personal_token(db):
    """Головне рішення task-78: конектор не отримує паралельної сутності."""
    user = _user(db)
    raw = _issue_access_token(db, user_id=user.id, client_id="cid-1", client=_client())
    db.flush()

    resolved = resolve_personal_token_sync(db, raw, settings=_settings())
    assert resolved is not None
    assert resolved.user_id == user.id
    assert resolved.origin == "agent"

    row = db.query(AgentToken).filter(AgentToken.user_id == user.id).one()
    assert row.oauth_client_id == "cid-1"
    assert row.label.startswith("Claude connector")
    lifetime_days = (row.expires_at - row.created_at).total_seconds() / 86400
    assert round(lifetime_days) == ACCESS_TOKEN_DAYS


def test_connector_never_gets_admin_rights_even_for_an_admin(db):
    """Адмін через чат — свідомо ні: батчі й керування людьми не мають
    бути на відстані однієї фрази до асистента."""
    admin = _user(db, email="admin-oauth@example.com", role=UserRole.admin)
    raw = _issue_access_token(db, user_id=admin.id, client_id="cid-1", client=_client())
    db.flush()

    resolved = resolve_personal_token_sync(db, raw, settings=_settings())
    assert resolved is not None
    assert resolved.role == UserRole.member


def test_viewer_connector_stays_read_only(db):
    viewer = _user(db, email="viewer-oauth@example.com", role=UserRole.viewer)
    raw = _issue_access_token(db, user_id=viewer.id, client_id="cid-1", client=_client())
    db.flush()

    resolved = resolve_personal_token_sync(db, raw, settings=_settings())
    assert resolved is not None
    assert resolved.role == UserRole.viewer


def test_deactivating_the_owner_kills_the_connector(db):
    user = _user(db, email="gone-oauth@example.com")
    raw = _issue_access_token(db, user_id=user.id, client_id="cid-1", client=_client())
    db.flush()
    assert resolve_personal_token_sync(db, raw, settings=_settings()) is not None

    user.is_active = False
    db.flush()
    assert resolve_personal_token_sync(db, raw, settings=_settings()) is None


@pytest.mark.parametrize("scopes", ["", SCOPE])
def test_refresh_token_is_never_stored_in_plaintext(db, scopes):
    user = _user(db, email=f"refresh-{len(scopes)}@example.com")
    raw = repo.issue_refresh_token(db, client_id="cid-1", user_id=user.id, scopes=scopes)
    row = repo.get_refresh_token(db, raw)
    assert row is not None
    assert raw not in row.token_sha256
