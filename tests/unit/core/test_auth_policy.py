"""Матриця ролей як чиста функція (`app/core/auth/policy.py`).

Тести в `tests/unit/api/test_auth_api.py` перевіряють той самий контракт
крізь живий middleware; тут — швидкий зріз самої таблиці, який показує
різницю «читати» / «запускати» без підняття застосунку і БД.
"""

from __future__ import annotations

import pytest

from app.core.auth.policy import admin_only_tool_slugs, required_role
from app.core.config import Settings
from app.schemas.enums import UserRole


@pytest.fixture
def settings() -> Settings:
    return Settings(auth_admin_only_paths=[])


BATCH = "/api/v1/reports/batches"


@pytest.mark.parametrize(
    "path",
    [
        BATCH,
        f"{BATCH}/9acbf91f-bad1-4f46-836f-d8f10614ce32",
        f"{BATCH}/9acbf91f-bad1-4f46-836f-d8f10614ce32/events",
        f"{BATCH}/9acbf91f-bad1-4f46-836f-d8f10614ce32/portfolio-table",
        f"{BATCH}/9acbf91f-bad1-4f46-836f-d8f10614ce32/portfolio.pdf",
        f"{BATCH}/9acbf91f-bad1-4f46-836f-d8f10614ce32/pdf-archive",
    ],
)
def test_reading_a_batch_is_open_to_every_signed_in_role(path: str, settings: Settings) -> None:
    assert required_role("GET", path, settings) is UserRole.viewer


@pytest.mark.parametrize(
    "method,path",
    [
        ("POST", BATCH),
        ("POST", f"{BATCH}/probe/cancel"),
        ("POST", f"{BATCH}/probe/clients/probe/retry"),
        ("DELETE", f"{BATCH}/probe"),
    ],
)
def test_changing_a_batch_stays_admin_only(method: str, path: str, settings: Settings) -> None:
    assert required_role(method, path, settings) is UserRole.admin


def test_fully_closing_batches_again_needs_no_code_change(settings: Settings) -> None:
    """Шлях назад до старої поведінки — конфіг, а не патч."""
    tightened = Settings(auth_admin_only_paths=[BATCH])
    assert required_role("GET", BATCH, tightened) is UserRole.admin
    assert required_role("GET", BATCH, settings) is UserRole.viewer


def test_the_other_admin_zones_stay_closed_for_reading(settings: Settings) -> None:
    for path in ("/api/v1/admin/users", "/api/v1/imports", "/api/v1/cloud/backups"):
        assert required_role("GET", path, settings) is UserRole.admin


def test_frontend_still_learns_that_starting_a_batch_is_admin_only(settings: Settings) -> None:
    assert "reports-batch" in admin_only_tool_slugs(settings)
