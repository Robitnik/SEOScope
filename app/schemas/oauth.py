"""DTO екрана згоди OAuth (task-78)."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class PendingAuthorizationDTO(BaseModel):
    request_id: str
    client_name: str
    redirect_host: str
    scopes: list[str]
    expires_at: datetime


class ConsentRequest(BaseModel):
    request_id: str = Field(min_length=1, max_length=64)


class ConsentResponse(BaseModel):
    redirect_to: str
