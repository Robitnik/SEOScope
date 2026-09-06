"""Auth/адмінка DTO (task-71).

Тут навмисно НЕМАЄ жодного поля з паролем у відповідях, окрім
`UserCreatedDTO.password` — єдиного місця в системі, де згенерований пароль
взагалі покидає процес, і рівно один раз (на створення або на перевидачу).
"""

from __future__ import annotations

import re
from datetime import datetime

from pydantic import BaseModel, Field, field_validator

from app.schemas.enums import UserRole

# Свідомо власна перевірка, а не `pydantic.EmailStr`: той тягне залежність
# `email-validator`, якої в образі немає, а `poetry lock` у цьому середовищі
# не проходить (сусідній чекаут AISeoTools). Для внутрішнього тулу, де адреси
# заводить адмін руками, достатньо синтаксичної перевірки.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]{2,}$")


def normalize_email(value: str) -> str:
    """Єдина канонізація адреси: trim + lowercase. Використовується і при
    створенні, і при логіні — інакше `Ivan@…` і `ivan@…` стали б двома
    різними обліковими записами."""
    email = value.strip().lower()
    if not _EMAIL_RE.match(email):
        raise ValueError("Invalid email address")
    return email


class UserDTO(BaseModel):
    id: str
    email: str
    name: str
    role: UserRole
    is_active: bool
    created_at: datetime
    last_login_at: datetime | None = None
    active_sessions: int = 0


class LoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=512)


class SessionDTO(BaseModel):
    """Відповідь `GET /api/v1/auth/session` — фронт стартує саме з неї."""

    authenticated: bool
    user: UserDTO | None = None
    csrf_token: str | None = None


class UserCreateRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    name: str = Field(min_length=1, max_length=200)
    role: UserRole = UserRole.member

    @field_validator("email")
    @classmethod
    def _email(cls, value: str) -> str:
        return normalize_email(value)


class UserUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    role: UserRole | None = None
    is_active: bool | None = None


class UserCreatedDTO(BaseModel):
    """Створений/перевиданий обліковий запис разом із паролем.

    Пароль показується рівно один раз: у БД лежить лише scrypt-хеш, у логах
    його немає (`RedactionFilter`), і жоден інший ендпоінт його не віддає.
    """

    user: UserDTO
    password: str


class UserSessionDTO(BaseModel):
    id: str
    created_at: datetime
    last_seen_at: datetime
    expires_at: datetime
    ip: str | None = None
    user_agent: str | None = None
    current: bool = False
