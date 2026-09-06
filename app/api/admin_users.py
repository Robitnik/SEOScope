"""Адмінка користувачів (task-71): створення, ролі, перевидача пароля, сесії.

Увесь префікс `/api/v1/admin` закритий для non-admin ще в `AuthMiddleware`;
`AdminActorDep` тут — другий, явний шар, який видно в сигнатурі й у OpenAPI.

Користувачів не видаляють — лише `is_active=False`. Видалення зробило б
історію дій (task-72) безавторською, а `created_by_id` — висячим.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api import register_router
from app.api.auth import _user_dto
from app.api.deps import AdminActorDep, SessionDep, SettingsDep
from app.core.auth.passwords import generate_password
from app.core.logging import get_logger
from app.db.repositories.users import UserRepository
from app.schemas.auth import (
    UserCreatedDTO,
    UserCreateRequest,
    UserDTO,
    UserSessionDTO,
    UserUpdateRequest,
)
from app.schemas.enums import UserRole

logger = get_logger(__name__)
router = APIRouter(prefix="/api/v1/admin/users", tags=["admin"])

_NOT_FOUND = {"error_code": "user_not_found", "message": "User not found"}


@router.get("")
async def list_users(session: SessionDep, actor: AdminActorDep) -> list[UserDTO]:
    def _list(db: Session) -> list[UserDTO]:
        repo = UserRepository(db)
        counts = repo.active_session_counts()
        return [_user_dto(user, sessions=counts.get(user.id, 0)) for user in repo.list_all()]

    return await session.run_sync(_list)


@router.post("", status_code=201)
async def create_user(
    payload: UserCreateRequest, session: SessionDep, actor: AdminActorDep
) -> UserCreatedDTO:
    """Генерує обліковий запис і повертає пароль **рівно один раз**.

    Далі його не дізнається ніхто: у БД лише scrypt-хеш, у логах його немає
    (`RedactionFilter`), інші ендпоінти його не віддають. Загублений пароль
    не «згадують», а перевидають (`/reset-password`).
    """
    password = generate_password()

    def _create(db: Session) -> UserDTO:
        repo = UserRepository(db)
        user = repo.create(
            email=payload.email,
            name=payload.name,
            role=payload.role,
            password=password,
            created_by_id=actor.id if actor.is_authenticated else None,
        )
        db.commit()
        return _user_dto(user)

    try:
        dto = await session.run_sync(_create)
    except IntegrityError:
        await session.rollback()
        raise HTTPException(
            status_code=409,
            detail={"error_code": "email_taken", "message": "This email already has an account"},
        ) from None

    logger.info("user_created email=%s role=%s by=%s", dto.email, dto.role.value, actor.email)
    return UserCreatedDTO(user=dto, password=password)


@router.patch("/{user_id}")
async def update_user(
    user_id: str, payload: UserUpdateRequest, session: SessionDep, actor: AdminActorDep
) -> UserDTO:
    def _update(db: Session) -> UserDTO:
        repo = UserRepository(db)
        user = repo.get(user_id)
        if user is None:
            raise HTTPException(status_code=404, detail=_NOT_FOUND)

        # Захист від «замкнути себе назовні»: адмін не знімає з себе роль і не
        # деактивує себе, і останнього активного адміна прибрати не можна —
        # інакше система лишається без нікого, хто заводить користувачів.
        losing_admin = (payload.role is not None and payload.role != UserRole.admin) or (
            payload.is_active is False
        )
        if user.role == UserRole.admin and losing_admin:
            if user.id == actor.id:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "error_code": "self_demotion",
                        "message": "You cannot remove your own admin access",
                    },
                )
            if repo.count_active_admins(excluding=user.id) == 0:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "error_code": "last_admin",
                        "message": "At least one active administrator must remain",
                    },
                )

        repo.update_profile(
            user, name=payload.name, role=payload.role, is_active=payload.is_active
        )
        if payload.is_active is False:
            # Деактивація має діяти негайно, а не «коли сесія протухне».
            repo.revoke_all_sessions(user.id)
        db.commit()
        counts = repo.active_session_counts()
        return _user_dto(user, sessions=counts.get(user.id, 0))

    dto = await session.run_sync(_update)
    logger.info("user_updated id=%s by=%s", user_id, actor.email)
    return dto


@router.post("/{user_id}/reset-password")
async def reset_password(
    user_id: str, session: SessionDep, actor: AdminActorDep
) -> UserCreatedDTO:
    """Новий згенерований пароль (знову показаний рівно один раз).

    Старі сесії при цьому відкликаються: скидання пароля — це або «людина
    його загубила», або «підозра на витік», і в обох випадках лишати живими
    відкриті сесії неправильно.
    """
    password = generate_password()

    def _reset(db: Session) -> UserDTO:
        repo = UserRepository(db)
        user = repo.get(user_id)
        if user is None:
            raise HTTPException(status_code=404, detail=_NOT_FOUND)
        repo.set_password(user, password)
        repo.revoke_all_sessions(user.id)
        db.commit()
        return _user_dto(user)

    dto = await session.run_sync(_reset)
    logger.info("user_password_reset id=%s by=%s", user_id, actor.email)
    return UserCreatedDTO(user=dto, password=password)


@router.post("/{user_id}/revoke-sessions")
async def revoke_sessions(
    user_id: str, session: SessionDep, actor: AdminActorDep
) -> dict[str, int]:
    def _revoke(db: Session) -> int:
        repo = UserRepository(db)
        if repo.get(user_id) is None:
            raise HTTPException(status_code=404, detail=_NOT_FOUND)
        revoked = repo.revoke_all_sessions(user_id)
        db.commit()
        return revoked

    revoked = await session.run_sync(_revoke)
    logger.info("user_sessions_revoked id=%s count=%s by=%s", user_id, revoked, actor.email)
    return {"revoked": revoked}


@router.get("/{user_id}/sessions")
async def user_sessions(
    user_id: str, request: Request, session: SessionDep, settings: SettingsDep, actor: AdminActorDep
) -> list[UserSessionDTO]:
    def _list(db: Session) -> list[UserSessionDTO]:
        repo = UserRepository(db)
        if repo.get(user_id) is None:
            raise HTTPException(status_code=404, detail=_NOT_FOUND)
        return [
            UserSessionDTO(
                id=row.id,
                created_at=row.created_at,
                last_seen_at=row.last_seen_at,
                expires_at=row.expires_at,
                ip=row.ip,
                user_agent=row.user_agent,
                current=row.id == actor.session_id,
            )
            for row in repo.active_sessions(user_id)
        ]

    return await session.run_sync(_list)


register_router(router)
