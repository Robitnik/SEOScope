"""DTO для `GET /api/v1/authorship*` (task-93 plan §2 D3).

Admin-only лукап автора на батч id: адмін бачить, хто запустив кожен звіт/
пакет, member/viewer — узагалі не викликають цей ендпоінт (`AuthMiddleware`
дасть 403, якщо все ж спробують). Тіла `GET /reports/runs` тощо не несуть
жодного з цих полів — див. `app.schemas.reports`/`app.schemas.
strategy_package` (рішення D3: рольово-інваріантні списки, бо тунель уже
живо віддавав закешовані `/api/*`-відповіді повз `no-store`).
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class AuthorUserDTO(BaseModel):
    id: str
    name: str
    email: str | None = None
    is_active: bool


class AuthorDTO(BaseModel):
    # `user | agent | local | cli | system | unknown` — той самий словник,
    # що `AuditEvent.actor_kind`, плюс `unknown` для рядків без автора
    # (до task-93 або неоднозначний бекфіл). `unknown`, а не `null`: явний
    # рядок читається у фронті так само, як решта станів.
    kind: str
    user: AuthorUserDTO | None = None


class AuthorshipItemDTO(BaseModel):
    resource_id: str
    author: AuthorDTO


class AuthorshipLookupDTO(BaseModel):
    resource_type: str
    items: list[AuthorshipItemDTO]


class AuthorshipFacetDTO(BaseModel):
    kind: str
    user: AuthorUserDTO | None = None
    count: int


class AuthorshipFacetsDTO(BaseModel):
    authors: list[AuthorshipFacetDTO] = Field(default_factory=list)
