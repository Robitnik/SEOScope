"""Особисті токени (task-79) — «кожен видає собі сам».

Усе тут — «про себе»: `ActorDep` дає особу з `AuthMiddleware` (сесія-cookie
чи вже наявний токен), і кожен запит скоупиться на `actor.id`. Немає
адмінського обходу «показати чужі токени» — журнал (task-72) і так бачить
усе, що зробив чужий токен, а перегляд чужих ключів поштучно не додав би
нічого корисного (план §1.1).

`SELF_SERVICE_PATHS` (`app/core/auth/policy.py`) відкриває мутуючі методи
цього префіксу для `viewer` — рівень ролі тут про факт автентифікації, не
про право писати чужі дані.

`Cache-Control: no-store` на кожній відповіді — знайдено вживу: жоден
ендпоінт застосунку не позначає JSON-відповіді як некешовані, і браузер
(або проксі між ним і нами) міг обслужити ЩОЙНО відкликаний токен зі свого
кешу на байт-у-байт ідентичний `GET`, роблячи «діє негайно» (план §3.2/§8)
хибним саме там, де це найдорожче помітити. `no-store` — найсуворіша
директива RFC 9111, не лише «звір заново», а «не зберігай узагалі».
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.orm import Session

from app.api import register_router
from app.api.deps import ActorDep, SessionDep
from app.core.auth.policy import min_role
from app.core.logging import get_logger
from app.db.models import AgentToken, User
from app.db.repositories.agent_tokens import AgentTokenRepository
from app.db.repositories.users import UserRepository
from app.schemas.me import CreateMyTokenRequest, CreateMyTokenResponse, MyTokenDTO

logger = get_logger(__name__)


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


router = APIRouter(prefix="/api/v1/me", tags=["me"], dependencies=[Depends(_no_store)])

_NOT_FOUND = {"error_code": "token_not_found", "message": "Token not found"}
_NOT_A_USER = {
    "error_code": "not_a_user",
    "message": "Personal tokens belong to a real account — sign in first.",
}


def _dto(row: AgentToken) -> MyTokenDTO:
    return MyTokenDTO(
        id=row.id,
        label=row.label,
        prefix=row.prefix,
        role=row.role,
        created_at=row.created_at,
        expires_at=row.expires_at,
        last_used_at=row.last_used_at,
        revoked_at=row.revoked_at,
    )


def _require_real_user(db: Session, actor_id: str) -> User:
    """`actor.id` буває `"agent"` (dev bearer, task-73, non-prod-only) чи
    `"system"` (`auth_enabled=False`) — рядка `users.id` з таким значенням
    не існує. Токен без реального власника не має сенсу (FK на `users`
    однаково б відмовив), тож відмовляємо тут явно, з поясненням, а не
    500-кою на порушений FK. Повертає ЖИВИЙ рядок, не знімок із сесії —
    роль-стеля нижче рахується від поточної ролі, не від тієї, що була на
    момент резолву сесії кілька рядків тому."""
    user = UserRepository(db).get(actor_id)
    if user is None:
        raise HTTPException(status_code=401, detail=_NOT_A_USER)
    return user


@router.get("/tokens")
async def list_my_tokens(session: SessionDep, actor: ActorDep) -> list[MyTokenDTO]:
    def _list(db: Session) -> list[MyTokenDTO]:
        user = _require_real_user(db, actor.id)
        return [_dto(row) for row in AgentTokenRepository(db).list_for_user(user.id)]

    return await session.run_sync(_list)


@router.post("/tokens", status_code=201)
async def create_my_token(
    payload: CreateMyTokenRequest, session: SessionDep, actor: ActorDep
) -> CreateMyTokenResponse:
    """Єдина відповідь із повним значенням токена — існує рівно тут.

    Роль обрізається до `min(запитана, роль власника)` (план §3.3): не
    можна видати собі більше, ніж маєш, але видати собі МЕНШЕ — заохочено.
    """

    def _create(db: Session) -> tuple[MyTokenDTO, str]:
        user = _require_real_user(db, actor.id)
        granted_role = min_role(payload.role, user.role)
        row, token = AgentTokenRepository(db).create(
            user_id=user.id,
            label=payload.label,
            role=granted_role,
            expires_in_days=payload.expires_in_days,
        )
        db.commit()
        return _dto(row), token

    dto, token = await session.run_sync(_create)
    logger.info("agent_token_created id=%s owner=%s label=%s", dto.id, actor.email, dto.label)
    return CreateMyTokenResponse(**dto.model_dump(), token=token)


@router.delete("/tokens/{token_id}", status_code=204)
async def revoke_my_token(token_id: str, session: SessionDep, actor: ActorDep) -> None:
    def _revoke(db: Session) -> None:
        user = _require_real_user(db, actor.id)
        repo = AgentTokenRepository(db)
        row = repo.get_for_user(token_id, user.id)
        if row is None:
            # 404, не 403: підтверджувати саме ІСНУВАННЯ чужого id — теж витік.
            raise HTTPException(status_code=404, detail=_NOT_FOUND)
        repo.revoke(row)
        db.commit()

    await session.run_sync(_revoke)
    logger.info("agent_token_revoked id=%s owner=%s", token_id, actor.email)


register_router(router)
