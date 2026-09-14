"""`GET /api/v1/authorship*` (task-93 plan §2 D3) — admin-only батчований
лукап автора.

Окремий ендпоінт, а не поле в `ReportRunDTO`/`ContentQaPackageDTO`: список
звітів має лишатись **байт-у-байт однаковим для кожної ролі**, бо
Cloudflare-тунель уже живо віддавав закешовані `/api/*`-відповіді повз
`no-store` (інцидент task-79, `Cache-Control` сам по собі це не рятує).
Рольове поле в спільному DTO означало б, що закешована версія адміна може
дістатись member'у. Тут навпаки: `Cache-Control: private, no-store` +
`Vary` — і сам факт, що ЛИШЕ адмін коли-небудь викликає цей шлях, той самий
принцип, що `app/api/me.py`'s `_no_store` (task-79).

`/api/v1/authorship` — у `DEFAULT_ADMIN_ONLY_PATHS` (`app/core/auth/
policy.py`), тож `AuthMiddleware` уже блокує non-admin за префіксом;
`AdminActorDep` тут — другий, явний шар, той самий патерн, що
`admin_users.py` (видно в сигнатурі й у OpenAPI).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.orm import Session

from app.api import register_router
from app.api.deps import AdminActorDep, SessionDep
from app.authorship.registry import RESOURCE_MODELS
from app.db.models import User
from app.db.repositories.authorship import (
    MAX_LOOKUP_IDS,
    AuthorFacet,
    AuthorInfo,
    AuthorshipRepository,
    AuthorshipRow,
    TooManyIdsError,
    UnknownResourceTypeError,
)
from app.schemas.api import ErrorDTO
from app.schemas.authorship import (
    AuthorDTO,
    AuthorshipFacetDTO,
    AuthorshipFacetsDTO,
    AuthorshipItemDTO,
    AuthorshipLookupDTO,
    AuthorUserDTO,
)


def _private_no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "private, no-store"
    # Varies by WHO is asking (cookie session or bearer token) — a shared
    # cache in front of the app (the Cloudflare tunnel, any future proxy)
    # must never treat two different callers' responses as interchangeable,
    # even if `no-store` above is honoured everywhere it should be.
    response.headers["Vary"] = "Cookie, Authorization"


router = APIRouter(
    prefix="/api/v1/authorship", tags=["authorship"], dependencies=[Depends(_private_no_store)]
)

_INVALID_RESOURCE_TYPE = ErrorDTO(
    error_code="invalid_resource_type",
    message=f"resource_type must be one of: {', '.join(sorted(RESOURCE_MODELS))}",
).model_dump()
_TOO_MANY_IDS = ErrorDTO(
    error_code="too_many_ids", message=f"at most {MAX_LOOKUP_IDS} ids per call"
).model_dump()


def _user_dto(user: User | None) -> AuthorUserDTO | None:
    if user is None:
        return None
    return AuthorUserDTO(id=user.id, name=user.name, email=user.email, is_active=user.is_active)


def _author_dto(info: AuthorInfo) -> AuthorDTO:
    return AuthorDTO(kind=info.kind or "unknown", user=_user_dto(info.user))


def _parse_ids(raw: str) -> list[str]:
    return [part for part in (piece.strip() for piece in raw.split(",")) if part]


@router.get("", response_model=AuthorshipLookupDTO)
async def lookup_authorship(
    session: SessionDep,
    actor: AdminActorDep,
    resource_type: str = Query(...),
    ids: str = Query(..., description="Comma-separated resource ids, at most 200"),
) -> AuthorshipLookupDTO:
    id_list = _parse_ids(ids)

    def _read(sync_session: Session) -> list[AuthorshipRow]:
        return AuthorshipRepository(sync_session).lookup(resource_type, id_list)

    try:
        rows = await session.run_sync(_read)
    except UnknownResourceTypeError as exc:
        raise HTTPException(422, _INVALID_RESOURCE_TYPE) from exc
    except TooManyIdsError as exc:
        raise HTTPException(422, _TOO_MANY_IDS) from exc

    return AuthorshipLookupDTO(
        resource_type=resource_type,
        items=[
            AuthorshipItemDTO(resource_id=row.resource_id, author=_author_dto(row.author))
            for row in rows
        ],
    )


@router.get("/facets", response_model=AuthorshipFacetsDTO)
async def authorship_facets(
    session: SessionDep,
    actor: AdminActorDep,
    resource_type: str = Query(
        ..., description="Comma-separated resource types, e.g. report_run,report_batch"
    ),
) -> AuthorshipFacetsDTO:
    types = _parse_ids(resource_type)

    def _read(sync_session: Session) -> list[AuthorFacet]:
        return AuthorshipRepository(sync_session).facets(types)

    try:
        facets = await session.run_sync(_read)
    except UnknownResourceTypeError as exc:
        raise HTTPException(422, _INVALID_RESOURCE_TYPE) from exc

    return AuthorshipFacetsDTO(
        authors=[
            AuthorshipFacetDTO(
                kind=facet.kind or "unknown", user=_user_dto(facet.user), count=facet.count
            )
            for facet in facets
        ]
    )


register_router(router)
