"""Adapter-boundary DTOs (PLAN §7): progress events, collection context/result, preflight.

These are the types every provider adapter (task-09/10/11) and the worker
(task-05) exchange through `app.integrations.contract.ProviderAdapter`.
`extra="forbid"` is set everywhere here on purpose: this is the most
sensitive-to-drift seam in the project, and a typo'd field should fail loud
rather than silently vanish.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from app.schemas.enums import (
    ComparisonMode,
    MetricUnit,
    PeriodKind,
    PeriodRole,
    Provider,
    Quality,
    SourceMode,
)


class ProgressEvent(BaseModel):
    """Emitted by an adapter during `collect()`.

    Fields are stable and structured; `message` is a redacted, human-readable
    string for display only — it is never a source of control-flow logic for
    the worker or UI.
    """

    model_config = ConfigDict(extra="forbid")

    provider: Provider
    stage: str
    completed: int | None = None
    total: int | None = None
    percent: float | None = None  # 0..100
    level: Literal["info", "warning", "error"] = "info"
    message: str = ""
    details: dict[str, Any] = {}


class DateRange(BaseModel):
    """Inclusive date range; the UI always renders the exact start/end."""

    model_config = ConfigDict(extra="forbid")

    start: date
    end: date


class CollectionContext(BaseModel):
    """Everything an adapter needs to run one `(client, provider)` cell."""

    model_config = ConfigDict(extra="forbid")

    client_id: str
    provider: Provider
    source_mode: SourceMode
    period_kind: PeriodKind
    comparison_mode: ComparisonMode
    current: DateRange
    previous: DateRange
    timezone: str  # e.g. "America/New_York"
    force_refresh: bool = False
    # Raw `SyncJob.mode` + `SyncJob.params_json` for adapters that branch on the
    # kind of run (task-25's tech_audit: a full crawl vs a targeted recheck).
    # Metric providers ignore both; defaults keep their behaviour unchanged.
    job_mode: str = ""
    job_params: dict[str, Any] = {}
    # Non-secret external identifiers only: property/account/project id,
    # database/country, report name. Never API keys, passwords, cookies.
    integration_config: dict[str, Any] = {}
    # Paths to mounted session/browser-profile files. The paths themselves
    # are not secrets; the files they point to must never enter the DB.
    session_paths: dict[str, str] = {}
    # Which parallel lane this cell runs in (task-31). For a browser provider
    # it selects the window to drive inside the ONE shared warm Chrome profile
    # — two concurrent cells of the same provider must not land on the same
    # window. `0` is the single-lane behaviour every adapter had before, so an
    # adapter that ignores this field keeps working unchanged.
    browser_slot: int = 0


class MetricValueResult(BaseModel):
    """One normalized metric value returned by an adapter, pre-persistence."""

    model_config = ConfigDict(extra="forbid")

    metric_key: str
    period_role: PeriodRole
    period_start: date | None = None  # None for snapshot metrics
    period_end: date | None = None
    value_numeric: Decimal | None = None  # None means "no data", never 0
    value_text: str | None = None
    unit: MetricUnit
    quality: Quality
    is_derived: bool = False


class RawArtifactRef(BaseModel):
    """Reference to a raw diagnostic artifact (JSON/CSV/PDF/screenshot)."""

    model_config = ConfigDict(extra="forbid")

    kind: str
    path: str  # relative, inside the artifacts root — never absolute
    sha256: str
    mime: str | None = None
    size_bytes: int | None = None


class CollectionResult(BaseModel):
    """Result of one `collect()` call.

    `collect()` never raises on partial data: any shortfall is expressed as
    `quality in {partial, truncated, unavailable}` plus `warnings`, not an
    exception. Only truly unexpected failures propagate, and the worker
    (task-05) still converts those into a `failed` cell rather than crashing.
    """

    model_config = ConfigDict(extra="forbid")

    provider: Provider
    quality: Quality  # aggregate quality across all metrics in this result
    observed_at: datetime  # UTC
    data_through: date | None = None  # e.g. GSC data lag
    metrics: list[MetricValueResult]
    warnings: list[str] = []
    error_code: str | None = None  # safe, stable code — no secrets, no traces
    provenance: dict[str, Any] = {}  # source_mode, AISeoTools commit, etc.
    artifacts: list[RawArtifactRef] = []
    units_spent: int | None = None
    units_estimated: int | None = None


class PreflightResult(BaseModel):
    """Result of `preflight()`: can this cell even attempt a collection?"""

    model_config = ConfigDict(extra="forbid")

    provider: Provider
    ok: bool
    # e.g. not_configured|auth_required|no_project|no_units|no_profile|...
    reason_code: str | None = None
    message: str = ""
    units_estimated: int | None = None
    details: dict[str, Any] = {}
