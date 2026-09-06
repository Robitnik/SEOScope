"""Логін/логаут/поточна сесія (task-71).

Свідомо НЕМАЄ ендпоінта зміни пароля: пароль генерує сервер і користувач його
не змінює (рішення оператора — так працюють інші тули компанії). Перевидати
може лише адмін через `/api/v1/admin/users/{id}/reset-password`.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Request, Response
from sqlalchemy.orm import Session

from app.api import register_router
from app.api.deps import ActorDep, SessionDep, SettingsDep
from app.core.auth.passwords import needs_rehash, verify_password, waste_time_like_verify
from app.core.auth.policy import admin_only_tool_slugs
from app.core.auth.sessions import AuthenticatedSession
from app.core.config import Settings
from app.core.logging import get_logger
from app.db.models import User
from app.db.repositories.users import UserRepository
from app.schemas.auth import LoginRequest, SessionDTO, UserDTO
from app.schemas.enums import UserRole

logger = get_logger(__name__)
router = APIRouter(prefix="/api/v1/auth", tags=["auth"])

# Єдиний текст на будь-яку невдачу входу: невідомий email, невірний пароль,
# деактивований або заблокований запис. Різні тексти перетворили б форму на
# оракул переліку співробітників агенції.
_INVALID = {"error_code": "invalid_credentials", "message": "Invalid email or password"}


def _user_dto(user: User, *, sessions: int = 0) -> UserDTO:
    return UserDTO(
        id=user.id,
        email=user.email,
        name=user.name,
        role=user.role,
        is_active=user.is_active,
        created_at=user.created_at,
        last_login_at=user.last_login_at,
        active_sessions=sessions,
    )


def set_session_cookies(
    response: Response, settings: Settings, *, token: str, csrf_token: str, max_age: int
) -> None:
    response.set_cookie(
        settings.auth_cookie_name,
        token,
        max_age=max_age,
        httponly=True,
        secure=settings.auth_cookie_secure,
        samesite="lax",
        path="/",
    )
    # НЕ HttpOnly: цей cookie фронт зчитує, щоб покласти значення в заголовок
    # `X-CSRF-Token`. Він ніколи не автентифікує сам по собі — лише звіряється
    # з тим, що лежить у сесії на сервері.
    response.set_cookie(
        settings.auth_csrf_cookie_name,
        csrf_token,
        max_age=max_age,
        httponly=False,
        secure=settings.auth_cookie_secure,
        samesite="lax",
        path="/",
    )


def clear_session_cookies(response: Response, settings: Settings) -> None:
    response.delete_cookie(settings.auth_cookie_name, path="/")
    response.delete_cookie(settings.auth_csrf_cookie_name, path="/")


@router.post("/login")
async def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    session: SessionDep,
    settings: SettingsDep,
) -> dict[str, object]:
    ip = request.client.host if request.client else None
    user_agent = request.headers.get("user-agent")

    def _authenticate(db: Session) -> tuple[User, str, str, str] | None:
        repo = UserRepository(db)
        user = repo.get_by_email(payload.email)
        if user is None:
            # Витрачаємо стільки ж часу, скільки коштувала б справжня
            # перевірка — інакше час відповіді сам відповідає на питання
            # «а чи є така людина в компанії».
            waste_time_like_verify()
            return None
        if not user.is_active or repo.is_locked(user):
            waste_time_like_verify()
            return None
        if not verify_password(payload.password, user.password_hash):
            repo.register_failure(
                user,
                max_failures=settings.login_max_failures,
                lockout_minutes=settings.login_lockout_minutes,
            )
            db.commit()
            return None
        if needs_rehash(user.password_hash):
            repo.set_password(user, payload.password)
        repo.register_success(user, now=datetime.now(UTC))
        session_row, token = repo.create_session(
            user,
            absolute_ttl_days=settings.session_absolute_ttl_days,
            ip=ip,
            user_agent=user_agent,
        )
        db.commit()
        return user, token, session_row.csrf_token, session_row.id

    result = await session.run_sync(_authenticate)
    if result is None:
        logger.info("login_failed email=%s ip=%s", payload.email, ip)
        response.status_code = 401
        return {"detail": _INVALID}

    user, token, csrf_token, session_id = result
    # Журнал (task-72) читає актора зі `request.state` ПІСЛЯ відповіді. На
    # самому логіні сесії ще не було, коли запит заходив, — але до моменту
    # виходу вона вже є, і подія `auth.login` має бути підписана людиною, а
    # не «anonymous».
    request.state.auth_session = AuthenticatedSession(
        user_id=user.id,
        email=user.email,
        name=user.name,
        role=user.role,
        is_active=user.is_active,
        session_id=session_id,
        csrf_token=csrf_token,
    )
    set_session_cookies(
        response,
        settings,
        token=token,
        csrf_token=csrf_token,
        max_age=settings.session_absolute_ttl_days * 24 * 3600,
    )
    logger.info("login_ok email=%s role=%s ip=%s", user.email, user.role.value, ip)
    return {
        "user": _user_dto(user).model_dump(mode="json"),
        "csrf_token": csrf_token,
        "admin_only_tools": admin_only_tool_slugs(settings),
    }


@router.post("/logout", status_code=204)
async def logout(
    request: Request,
    response: Response,
    session: SessionDep,
    settings: SettingsDep,
    actor: ActorDep,
) -> Response:
    token = request.cookies.get(settings.auth_cookie_name)
    if token:

        def _revoke(db: Session) -> None:
            repo = UserRepository(db)
            row = repo.get_session_by_token(token)
            if row is not None:
                repo.revoke_session(row)
                db.commit()

        await session.run_sync(_revoke)
    clear_session_cookies(response, settings)
    response.status_code = 204
    return response


@router.post("/logout-all", status_code=204)
async def logout_all(
    response: Response, session: SessionDep, settings: SettingsDep, actor: ActorDep
) -> Response:
    """Вийти на всіх пристроях — для випадку «залогінився на чужому ноуті»."""
    if actor.is_authenticated:

        def _revoke_all(db: Session) -> None:
            UserRepository(db).revoke_all_sessions(actor.id)
            db.commit()

        await session.run_sync(_revoke_all)
    clear_session_cookies(response, settings)
    response.status_code = 204
    return response


@router.get("/session")
async def current_session(
    request: Request, session: SessionDep, settings: SettingsDep
) -> dict[str, object]:
    """Публічний ендпоінт: фронт стартує саме з нього і за `authenticated`
    вирішує, малювати додаток чи вести на форму логіну."""
    if not settings.auth_enabled:
        return {
            "authenticated": True,
            "user": UserDTO(
                id="system",
                email="auth-disabled@localhost",
                name="Auth disabled",
                role=UserRole.admin,
                is_active=True,
                created_at=datetime.now(UTC),
            ).model_dump(mode="json"),
            "csrf_token": None,
            "admin_only_tools": admin_only_tool_slugs(settings),
        }

    token = request.cookies.get(settings.auth_cookie_name)
    if not token:
        return SessionDTO(authenticated=False).model_dump(mode="json")

    def _load(db: Session) -> dict[str, object] | None:
        repo = UserRepository(db)
        row = repo.get_session_by_token(token)
        if row is None or row.revoked_at is not None:
            return None
        user = repo.get(row.user_id)
        if user is None or not user.is_active:
            return None
        return {
            "user": _user_dto(user).model_dump(mode="json"),
            "csrf_token": row.csrf_token,
        }

    loaded = await session.run_sync(_load)
    if loaded is None:
        return SessionDTO(authenticated=False).model_dump(mode="json")
    return {
        "authenticated": True,
        **loaded,
        "admin_only_tools": admin_only_tool_slugs(settings),
    }


register_router(router)
