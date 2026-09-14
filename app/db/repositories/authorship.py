"""Читання авторства для `GET /api/v1/authorship*` (task-93 plan §2 D3).

Admin-only surface: batched lookup of `(created_by_kind, created_by_user_id)`
across one resource type, joined against `users` for the current name/email/
`is_active` (NOT denormalized like `audit_events` — task-93's author needs
the person's CURRENT state, unlike the journal's historical snapshot).
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.authorship.registry import resolve_model
from app.db.models import User

MAX_LOOKUP_IDS = 200


class UnknownResourceTypeError(ValueError):
    """`resource_type` isn't in `app.authorship.registry.RESOURCE_MODELS`."""


class TooManyIdsError(ValueError):
    """More than `MAX_LOOKUP_IDS` requested in one call."""


@dataclass(frozen=True)
class AuthorInfo:
    """`kind=None` here means "no author recorded" — the API layer renders
    that as the literal string `"unknown"`; kept as `None` internally so it
    is never confused with a resource type that has a `kind="unknown"`
    filter value stored somewhere (it never is — see `app.db.repositories.
    reports.apply_authorship_filter`)."""

    kind: str | None
    user: User | None


@dataclass(frozen=True)
class AuthorshipRow:
    resource_id: str
    author: AuthorInfo


@dataclass(frozen=True)
class AuthorFacet:
    kind: str | None
    user: User | None
    count: int


class AuthorshipRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def _users_by_id(self, user_ids: set[str]) -> dict[str, User]:
        if not user_ids:
            return {}
        rows = self._session.scalars(select(User).where(User.id.in_(user_ids)))
        return {u.id: u for u in rows}

    def lookup(self, resource_type: str, ids: list[str]) -> list[AuthorshipRow]:
        """Rows for ids that actually exist, in no particular order — the
        caller (the API layer) is responsible for `422` on an unknown
        `resource_type` or too many ids; this raises the same two errors so
        a direct caller (a future script, a test) gets them too."""
        if len(ids) > MAX_LOOKUP_IDS:
            raise TooManyIdsError(f"at most {MAX_LOOKUP_IDS} ids per call, got {len(ids)}")
        model = resolve_model(resource_type)
        if model is None:
            raise UnknownResourceTypeError(resource_type)
        if not ids:
            return []

        stmt = select(model.id, model.created_by_kind, model.created_by_user_id).where(
            model.id.in_(ids)
        )
        rows = self._session.execute(stmt).all()
        users = self._users_by_id({uid for _id, _kind, uid in rows if uid is not None})
        return [
            AuthorshipRow(
                resource_id=resource_id,
                author=AuthorInfo(kind=kind, user=users.get(user_id) if user_id else None),
            )
            for resource_id, kind, user_id in rows
        ]

    def facets(self, resource_types: list[str]) -> list[AuthorFacet]:
        """Distinct `(kind, user_id)` pairs across every requested resource
        type with how many rows carry each — the filter dropdown's option
        list (task-93 plan §2 D3's `facets` response)."""
        counts: Counter[tuple[str | None, str | None]] = Counter()
        for resource_type in resource_types:
            model = resolve_model(resource_type)
            if model is None:
                raise UnknownResourceTypeError(resource_type)
            stmt = select(model.created_by_kind, model.created_by_user_id)
            for kind, user_id in self._session.execute(stmt).all():
                counts[(kind, user_id)] += 1

        users = self._users_by_id({uid for (_kind, uid) in counts if uid is not None})
        return [
            AuthorFacet(kind=kind, user=users.get(user_id) if user_id else None, count=count)
            for (kind, user_id), count in counts.items()
        ]
