"""DTO для `/api/v1/me/tokens` (task-79) — особисті токени."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.enums import UserRole

# Немає варіанту «безстроково» — навмисно (план §3.4): забутий вічний токен
# у чужому конфізі є найтихішим способом втратити доступ назавжди.
TokenLifetimeDays = Literal[30, 90, 365]


class MyTokenDTO(BaseModel):
    """Один рядок списку — НІКОЛИ не несе повного значення, лише `prefix`."""

    id: str
    label: str
    prefix: str
    role: UserRole
    created_at: datetime
    expires_at: datetime
    last_used_at: datetime | None
    revoked_at: datetime | None


class CreateMyTokenRequest(BaseModel):
    label: str = Field(min_length=1, max_length=200)
    role: UserRole = UserRole.member
    expires_in_days: TokenLifetimeDays = 90


class CreateMyTokenResponse(MyTokenDTO):
    """Єдина відповідь, де є повне значення — існує рівно тут і ніде більше."""

    token: str
