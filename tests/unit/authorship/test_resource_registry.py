"""`app.authorship.registry` (task-93 plan §2 D3): the explicit resource-type
allowlist `GET /api/v1/authorship` accepts."""

from __future__ import annotations

from app.authorship.registry import RESOURCE_MODELS, resolve_model
from app.db.models import ContentQaPackage, ReportBatch, ReportRun


def test_resolves_every_documented_resource_type() -> None:
    assert resolve_model("report_run") is ReportRun
    assert resolve_model("report_batch") is ReportBatch
    assert resolve_model("content_qa_package") is ContentQaPackage


def test_unknown_resource_type_resolves_to_none() -> None:
    assert resolve_model("client") is None
    assert resolve_model("") is None


def test_every_registered_model_carries_the_authorship_columns() -> None:
    """Defensive: if a future edit adds a resource type whose model forgot
    the two `created_by_*` columns, this fails loudly here instead of as an
    `AttributeError` deep inside `AuthorshipRepository`."""
    for model in RESOURCE_MODELS.values():
        assert hasattr(model, "created_by_kind")
        assert hasattr(model, "created_by_user_id")
