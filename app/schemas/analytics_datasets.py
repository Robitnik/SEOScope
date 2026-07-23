"""Shared contract for provider-specific analytical dataset rows."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict

from app.schemas.enums import PeriodRole, Provider, Quality

DatasetScalar = Any


class AnalyticsDatasetValueResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dataset_key: str
    dimension_key: str
    dimension_value: str
    period_role: PeriodRole
    period_start: date
    period_end: date
    metrics: dict[str, DatasetScalar]
    quality: Quality
    rank: int | None = None


class AnalyticsDatasetCollectionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: Provider
    quality: Quality
    observed_at: datetime
    rows: list[AnalyticsDatasetValueResult] = []
    warnings: list[str] = []
    error_code: str | None = None
    provenance: dict[str, Any] = {}
