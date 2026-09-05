"""DTO для Schema Markup Checker (task-56).

Дзеркалить форму `content_qa.py`: create-запит на URL, summary для списку
минулих прогонів, повний DTO для сторінки одного звіту з shareable-лінком
(`#/tools/schema-markup/<id>`).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel

SchemaMarkupVerdict = Literal["clean", "has_gaps", "missing", "fetch_failed"]
SchemaMarkupSeverity = Literal["error", "warn", "info"]
SchemaMarkupLevel = Literal["L0", "L1", "L2"]
SchemaMarkupRawFormat = Literal["json-ld", "microdata"]


class SchemaMarkupCheckRequest(BaseModel):
    url: str
    client_id: str | None = None


class SchemaMarkupFindingDTO(BaseModel):
    level: SchemaMarkupLevel
    severity: SchemaMarkupSeverity
    code: str
    message: str
    entity_types: list[str] = []
    property_name: str | None = None


class SchemaMarkupGeneratedEntityDTO(BaseModel):
    type_name: str
    # `jsonld` — готовий до вставки JSON-LD вузол ({"@context": ..., "@type": ..., ...}).
    jsonld: dict[str, Any]
    # source per property name — де взято значення (page:title, url_root, client:name, …).
    sources: dict[str, str] = {}
    needs_input: list[str] = []


class SchemaMarkupGeneratedDTO(BaseModel):
    entities: list[SchemaMarkupGeneratedEntityDTO] = []
    # (type_name, reason) — чому тип не запропоновано (вже є / заборонений / нема даних).
    skipped: list[list[str]] = []


class SchemaMarkupRawBlockDTO(BaseModel):
    """Розмітка як вона реально лежить на сторінці — окремо від `found_types`
    (лише список імен) і `findings` (лише порушення). `raw` для `json-ld` —
    оригінальний вузол/граф, як його написав плагін; для `microdata` — те саме
    дерево, зібране з `itemprop`/`itemscope` (нативної JSON-форми в microdata
    немає, тому дерево серіалізується — `ExtractedEntity.to_jsonable()`).
    """

    format: SchemaMarkupRawFormat
    raw: Any = None
    # Лише для `json-ld`: блок був присутній, але не розпарсився (L0).
    error: str | None = None


class SchemaMarkupCheckSummaryDTO(BaseModel):
    id: str
    client_id: str | None = None
    client_name: str | None = None
    url: str
    final_url: str | None = None
    verdict: SchemaMarkupVerdict
    page_kind: str | None = None
    found_types: list[str] = []
    created_at: datetime


class SchemaMarkupCheckDTO(SchemaMarkupCheckSummaryDTO):
    http_status: int | None = None
    findings: list[SchemaMarkupFindingDTO] = []
    generated: SchemaMarkupGeneratedDTO | None = None
    raw_markup: list[SchemaMarkupRawBlockDTO] = []
    vocab_version: str
    error: str | None = None


class SchemaMarkupChecksPageDTO(BaseModel):
    total: int
    limit: int
    offset: int
    checks: list[SchemaMarkupCheckSummaryDTO]
