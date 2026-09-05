"""DTOs for the Page Inventory table (task-57), prefix
`/api/v1/clients/{client_id}/pages`.

Same conventions as `app/schemas/client_monthly.py`: `Literal[...]` aliases
instead of `enum.Enum`, read DTOs left open (no `extra="forbid"`).
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel

QaProfile = Literal["calibrated", "strict"]
Severity = Literal["error", "warn", "info"]
StatusLabel = Literal["live", "broken", "redirect", "noindex", "not_checked"]
BreadcrumbSource = Literal["json_ld", "dom", "mixed", "none"]
KeywordsSource = Literal["meta", "none"]


class BreadcrumbNodeDTO(BaseModel):
    name: str
    url: str | None = None


class FindingDTO(BaseModel):
    check: str
    severity: Severity
    field: str
    detail: str
    value: str | None = None


class PageInventoryRowDTO(BaseModel):
    url: str
    canonical_key: str
    duplicate_urls: list[str]
    page_label: str
    breadcrumbs: list[BreadcrumbNodeDTO]
    breadcrumb_source: BreadcrumbSource
    title: str | None
    description: str | None
    keywords: list[str]
    keywords_source: KeywordsSource
    h1: str | None
    status: int | None
    status_label: StatusLabel
    indexable: bool | None
    canonical_missing: bool
    checked: bool
    # Findings for THIS row, already calibrated to the requested `?profile=`.
    findings: list[FindingDTO]
    findings_by_field: dict[str, list[FindingDTO]]
    worst_severity: Severity | None


class DuplicateClusterDTO(BaseModel):
    check: str
    field: str
    members: list[str]


class PageInventorySummaryDTO(BaseModel):
    client_id: str
    built_at: datetime | None
    source_collected_at: datetime | None
    pages_total: int
    pages_unique: int
    pages_with_breadcrumbs: int
    pages_with_keywords: int
    breadcrumb_source: BreadcrumbSource
    duplicate_groups: int
    pages_in_duplicate_groups: int
    severity_counts: dict[Severity, int]
    warnings: list[str]
    never_built: bool
    stale: bool


class PageInventoryTableDTO(BaseModel):
    summary: PageInventorySummaryDTO
    rows: list[PageInventoryRowDTO]
    duplicate_clusters: list[DuplicateClusterDTO]
    total: int
    page: int
    page_size: int
    profile: QaProfile


class PageInventoryRebuildResponseDTO(BaseModel):
    table: PageInventoryTableDTO
