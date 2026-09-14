"""Which resource types `GET /api/v1/authorship` knows how to look up
(task-93 plan §2 D3).

A small, explicit map rather than reflection over every model with
`created_by_*` columns — the endpoint is a public-ish (admin-only) surface,
so the set of resource types it accepts is a deliberate choice, not
"whatever happens to have the columns today".
"""

from __future__ import annotations

from app.db.models import ContentQaPackage, ReportBatch, ReportRun

# A plain `type` return would lose every attribute mypy needs to check
# `AuthorshipRepository`'s queries (`.id`/`.created_by_kind`/
# `.created_by_user_id`) — this union is exactly the three model classes
# below, kept in sync with `RESOURCE_MODELS` by construction.
AuthorableModel = type[ReportRun] | type[ReportBatch] | type[ContentQaPackage]

RESOURCE_MODELS: dict[str, AuthorableModel] = {
    "report_run": ReportRun,
    "report_batch": ReportBatch,
    "content_qa_package": ContentQaPackage,
}


def resolve_model(resource_type: str) -> AuthorableModel | None:
    return RESOURCE_MODELS.get(resource_type)
