"""Declarative base, naming convention and portable column types (PLAN §6).

SQLite has no native UUID/Decimal/timezone-aware-datetime storage, so this
module defines small `TypeDecorator`s that store a canonical representation
(text UUID, text-encoded Decimal, UTC-naive-on-disk datetime) and always hand
back the right Python type to callers. Repositories (and nothing above them)
are the only code that should import from here directly.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import DateTime, MetaData, String, TypeDecorator
from sqlalchemy.orm import DeclarativeBase

# Alembic autogenerate needs explicit names for every constraint kind, or it
# invents unstable ones that differ across runs and make migrations diff-noisy.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class GUID(TypeDecorator[str]):
    """Text-stored UUID primary/foreign key. Always round-trips as `str`."""

    impl = String(36)
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Any) -> str | None:
        if value is None:
            return None
        return str(value)

    def process_result_value(self, value: Any, dialect: Any) -> str | None:
        if value is None:
            return None
        return str(value)


def new_uuid() -> str:
    return str(uuid.uuid4())


class UTCDateTime(TypeDecorator[datetime]):
    """Timezone-aware UTC datetime. Naive input is rejected, not guessed."""

    impl = DateTime(timezone=False)
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Any) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("UTCDateTime requires a timezone-aware datetime")
        naive: datetime = value.astimezone(UTC).replace(tzinfo=None)
        return naive

    def process_result_value(self, value: Any, dialect: Any) -> datetime | None:
        if value is None:
            return None
        aware: datetime = value.replace(tzinfo=UTC)
        return aware


def utcnow() -> datetime:
    return datetime.now(UTC)


class DecimalString(TypeDecorator[Decimal]):
    """Exact-precision Decimal stored as text — never binary float."""

    impl = String(64)
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Any) -> str | None:
        if value is None:
            return None
        return str(Decimal(value))

    def process_result_value(self, value: Any, dialect: Any) -> Decimal | None:
        if value is None:
            return None
        return Decimal(value)
