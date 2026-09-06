"""Екран згоди для remote-конектора (task-78).

Розділення обов'язків тут навмисне. `/authorize` (маршрут SDK, зареєстрований
у `app/mcp/asgi.py`) розуміє OAuth, але нічого не знає ні про наш логін, ні
про наші ролі; він лише складає намір у `oauth_pending_authorizations` і веде
браузер на `#/oauth/consent`. Ці два ендпоінти — навпаки: нічого не знають
про OAuth, лише «покажи людині, хто просить доступ» і «людина погодилась».

Через це `POST /consent` вимагає ЖИВОЇ cookie-сесії: код авторизації
прив'язується до `actor.id`, тобто до тієї людини, яка реально сиділа за
браузером і натиснула Approve. Підмінити цей зв'язок параметром запиту
неможливо — `user_id` беремо з сесії, а не з тіла.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.orm import Session

from app.api import register_router
from app.api.deps import ActorDep, SessionDep
from app.core.logging import get_logger
from app.db.repositories import oauth as repo
from app.db.repositories.users import UserRepository
from app.mcp.oauth import build_redirect
from app.schemas.oauth import ConsentRequest, ConsentResponse, PendingAuthorizationDTO

logger = get_logger(__name__)


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


router = APIRouter(prefix="/api/v1/oauth", tags=["oauth"], dependencies=[Depends(_no_store)])

_GONE = {
    "error_code": "authorization_expired",
    "message": "This connection request expired. Start again from Claude.",
}
_NOT_A_USER = {
    "error_code": "not_a_user",
    "message": "Connecting binds the connector to a real account — sign in first.",
}


def _client_name(db: Session, client_id: str) -> str:
    payload = repo.load_client(db, client_id)
    if not payload:
        return client_id
    name = payload.get("client_name")
    return str(name) if name else client_id


@router.get("/pending/{request_id}", response_model=PendingAuthorizationDTO)
async def get_pending(
    request_id: str, session: SessionDep, actor: ActorDep
) -> PendingAuthorizationDTO:
    def _load(db: Session) -> PendingAuthorizationDTO:
        row = repo.get_pending(db, request_id)
        if row is None:
            raise HTTPException(status_code=410, detail=_GONE)
        return PendingAuthorizationDTO(
            request_id=row.id,
            client_name=_client_name(db, row.client_id),
            # Куди саме полетить код — єдине, що дійсно варто показати
            # людині перед Approve: MCP-специфікація прямо вимагає
            # показувати хост redirect_uri, бо саме він, а не назва
            # клієнта, є неспотворюваним.
            redirect_host=row.redirect_uri,
            scopes=row.scopes.split(),
            expires_at=row.expires_at,
        )

    return await session.run_sync(_load)


@router.post("/consent", response_model=ConsentResponse)
async def approve(
    payload: ConsentRequest, session: SessionDep, actor: ActorDep
) -> ConsentResponse:
    def _approve(db: Session) -> ConsentResponse:
        # `actor.id` буває `"agent"`/`"local"`/`"system"` (task-73) —
        # рядка `users.id` з таким значенням не існує, а код авторизації
        # мусить належати справжній людині, інакше FK однаково відмовить
        # 500-кою.
        user = UserRepository(db).get(actor.id)
        if user is None or not user.is_active:
            raise HTTPException(status_code=401, detail=_NOT_A_USER)
        row = repo.get_pending(db, payload.request_id)
        if row is None:
            raise HTTPException(status_code=410, detail=_GONE)
        code = repo.issue_auth_code(db, pending=row, user_id=actor.id)
        logger.info("oauth_consent_granted client=%s user=%s", row.client_id, actor.id)
        return ConsentResponse(redirect_to=build_redirect(row, code))

    result = await session.run_sync(_approve)
    await session.commit()
    return result


register_router(router)
