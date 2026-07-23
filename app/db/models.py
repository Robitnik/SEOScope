"""ORM models for all tables in PLAN.md §6.

Only foreign keys are modeled — no `relationship()` attributes. Repositories
own every query and transaction boundary (CLAUDE.md: "no raw SQL in the
upper layers"); ORM-level lazy-loading traversal would bypass that boundary
and is easy to get wrong under the async engine, so it is deliberately not
offered here.
"""

from __future__ import annotations

import enum
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    event,
)
from sqlalchemy import (
    Enum as SAEnum,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import GUID, Base, DecimalString, UTCDateTime, new_uuid, utcnow
from app.schemas.enums import (
    CellStatus,
    ComparisonMode,
    JobStatus,
    MetricUnit,
    PeriodKind,
    PeriodRole,
    Provider,
    Quality,
    SourceMode,
    StageStatus,
)


def _str_enum(enum_cls: type[enum.Enum], length: int = 32) -> SAEnum:
    """SQLite has no native enum type: store `.value` in a CHECK-constrained VARCHAR."""
    return SAEnum(enum_cls, values_callable=lambda obj: [e.value for e in obj], length=length)


class Client(Base):
    __tablename__ = "clients"
    __table_args__ = (UniqueConstraint("slug", name="uq_clients_slug"),)

    id: Mapped[str] = mapped_column(GUID, primary_key=True, default=new_uuid)
    name: Mapped[str] = mapped_column(String(200))
    slug: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    domain: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    logo_path: Mapped[str | None] = mapped_column(String(500), default=None)
    market: Mapped[str | None] = mapped_column(String(100), default=None)
    timezone: Mapped[str] = mapped_column(String(64), default="America/New_York")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


class ClientIntegration(Base):
    __tablename__ = "client_integrations"
    __table_args__ = (
        UniqueConstraint("client_id", "provider", name="uq_client_integrations_client_id_provider"),
    )

    id: Mapped[str] = mapped_column(GUID, primary_key=True, default=new_uuid)
    client_id: Mapped[str] = mapped_column(
        GUID, ForeignKey("clients.id", ondelete="CASCADE"), index=True
    )
    provider: Mapped[Provider] = mapped_column(_str_enum(Provider))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    source_mode: Mapped[SourceMode] = mapped_column(_str_enum(SourceMode), default=SourceMode.auto)
    # Non-secret external identifiers only (property/account/project id,
    # database/country, report name) — mirrors CollectionContext.integration_config.
    # API keys/passwords/session cookies must never be written here.
    config_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    last_success_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)
    last_attempt_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


class SyncJob(Base):
    __tablename__ = "sync_jobs"

    id: Mapped[str] = mapped_column(GUID, primary_key=True, default=new_uuid)
    batch_id: Mapped[str | None] = mapped_column(GUID, index=True, default=None)
    client_id: Mapped[str] = mapped_column(
        GUID, ForeignKey("clients.id", ondelete="CASCADE"), index=True
    )
    period_kind: Mapped[PeriodKind] = mapped_column(_str_enum(PeriodKind))
    comparison_mode: Mapped[ComparisonMode] = mapped_column(_str_enum(ComparisonMode))
    anchor_date: Mapped[date] = mapped_column(Date)
    custom_start: Mapped[date | None] = mapped_column(Date, default=None)
    custom_end: Mapped[date | None] = mapped_column(Date, default=None)
    requested_providers_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    # "collect_missing" | "force_refresh" (PLAN §9.2, SyncJobRequest.mode) — read
    # by the job engine (task-05) when deciding whether a cell with a fresh
    # snapshot can be skipped instead of re-collected.
    mode: Mapped[str] = mapped_column(String(32), default="collect_missing")
    # Optional, non-secret job parameters for run kinds that need them beyond the
    # period fields (task-25 tech_audit recheck: the source run dir + the exact
    # (slug, url) findings to re-verify). NULL for ordinary metric-provider syncs.
    params_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    status: Mapped[JobStatus] = mapped_column(
        _str_enum(JobStatus), default=JobStatus.queued, index=True
    )
    progress_percent: Mapped[float | None] = mapped_column(Float, default=0.0)
    current_stage: Mapped[str | None] = mapped_column(String(64), default=None)
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    idempotency_key: Mapped[str | None] = mapped_column(
        String(255), unique=True, index=True, default=None
    )
    lease_owner: Mapped[str | None] = mapped_column(String(200), default=None)
    lease_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)
    heartbeat_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)
    error_code: Mapped[str | None] = mapped_column(String(100), default=None)
    error_message: Mapped[str | None] = mapped_column(String(1000), default=None)


class SyncJobProvider(Base):
    """One `(client, provider)` collection cell — the atomic unit of PLAN §9.1."""

    __tablename__ = "sync_job_providers"
    __table_args__ = (
        UniqueConstraint("job_id", "provider", name="uq_sync_job_providers_job_id_provider"),
    )

    id: Mapped[str] = mapped_column(GUID, primary_key=True, default=new_uuid)
    job_id: Mapped[str] = mapped_column(
        GUID, ForeignKey("sync_jobs.id", ondelete="CASCADE"), index=True
    )
    batch_id: Mapped[str | None] = mapped_column(GUID, default=None)
    client_id: Mapped[str] = mapped_column(GUID, ForeignKey("clients.id", ondelete="CASCADE"))
    provider: Mapped[Provider] = mapped_column(_str_enum(Provider))
    status: Mapped[CellStatus] = mapped_column(_str_enum(CellStatus), default=CellStatus.queued)
    progress_percent: Mapped[float | None] = mapped_column(Float, default=0.0)
    current_stage: Mapped[str | None] = mapped_column(String(64), default=None)
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    lease_owner: Mapped[str | None] = mapped_column(String(200), default=None)
    lease_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)
    heartbeat_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)
    # `(provider, profile_name)` browser/session lock key — PLAN §8.3.
    profile_key: Mapped[str | None] = mapped_column(String(255), default=None)
    units_spent: Mapped[int | None] = mapped_column(Integer, default=None)
    units_estimated: Mapped[int | None] = mapped_column(Integer, default=None)
    error_code: Mapped[str | None] = mapped_column(String(100), default=None)
    error_message: Mapped[str | None] = mapped_column(String(1000), default=None)
    last_success_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)


# Selecting the next queued cell for a lane and aggregating a batch's
# progress both filter on these pairs without joining `sync_jobs`.
Index("ix_sync_job_providers_provider_status", SyncJobProvider.provider, SyncJobProvider.status)
Index("ix_sync_job_providers_batch_id_status", SyncJobProvider.batch_id, SyncJobProvider.status)


class SyncJobStep(Base):
    __tablename__ = "sync_job_steps"
    __table_args__ = (
        UniqueConstraint(
            "job_id",
            "provider",
            "stage",
            "attempt",
            name="uq_sync_job_steps_job_provider_stage_attempt",
        ),
    )

    id: Mapped[str] = mapped_column(GUID, primary_key=True, default=new_uuid)
    job_id: Mapped[str] = mapped_column(
        GUID, ForeignKey("sync_jobs.id", ondelete="CASCADE"), index=True
    )
    provider: Mapped[Provider] = mapped_column(_str_enum(Provider))
    stage: Mapped[str] = mapped_column(String(64))
    status: Mapped[StageStatus] = mapped_column(_str_enum(StageStatus), default=StageStatus.pending)
    progress_percent: Mapped[float | None] = mapped_column(Float, default=None)
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)
    warning_summary: Mapped[str | None] = mapped_column(String(1000), default=None)
    error_summary: Mapped[str | None] = mapped_column(String(1000), default=None)
    units_spent: Mapped[int | None] = mapped_column(Integer, default=None)
    units_estimated: Mapped[int | None] = mapped_column(Integer, default=None)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class JobEvent(Base):
    """`seq` is a global monotonic autoincrement and doubles as the SSE event id."""

    __tablename__ = "job_events"
    __table_args__ = (Index("ix_job_events_job_id_seq", "job_id", "seq"),)

    seq: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(
        GUID, ForeignKey("sync_jobs.id", ondelete="CASCADE"), index=True
    )
    timestamp: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    level: Mapped[str] = mapped_column(String(16), default="info")
    provider: Mapped[Provider | None] = mapped_column(_str_enum(Provider), default=None)
    stage: Mapped[str | None] = mapped_column(String(64), default=None)
    percent: Mapped[float | None] = mapped_column(Float, default=None)
    message: Mapped[str] = mapped_column(String(2000), default="")
    details_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class ProviderSnapshot(Base):
    """Immutable after commit — see the `before_update` guard below."""

    __tablename__ = "provider_snapshots"

    id: Mapped[str] = mapped_column(GUID, primary_key=True, default=new_uuid)
    job_id: Mapped[str] = mapped_column(
        GUID, ForeignKey("sync_jobs.id", ondelete="CASCADE"), index=True
    )
    client_id: Mapped[str] = mapped_column(
        GUID, ForeignKey("clients.id", ondelete="CASCADE"), index=True
    )
    provider: Mapped[Provider] = mapped_column(_str_enum(Provider), index=True)
    source_mode: Mapped[SourceMode] = mapped_column(_str_enum(SourceMode))
    source_version: Mapped[str | None] = mapped_column(String(200), default=None)
    observed_at: Mapped[datetime] = mapped_column(UTCDateTime)
    data_through: Mapped[date | None] = mapped_column(Date, default=None)
    quality: Mapped[Quality] = mapped_column(_str_enum(Quality))
    warnings_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    provenance_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    raw_artifact_id: Mapped[str | None] = mapped_column(
        GUID, ForeignKey("artifacts.id", ondelete="SET NULL"), default=None
    )
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class MetricValue(Base):
    """Immutable after commit — see the `before_update` guard below."""

    __tablename__ = "metric_values"
    __table_args__ = (
        UniqueConstraint(
            "snapshot_id", "metric_key", "period_role", name="uq_metric_values_snapshot_metric_role"
        ),
        Index("ix_metric_values_client_provider_metric", "client_id", "provider", "metric_key"),
        Index("ix_metric_values_period", "period_start", "period_end"),
    )

    id: Mapped[str] = mapped_column(GUID, primary_key=True, default=new_uuid)
    snapshot_id: Mapped[str] = mapped_column(
        GUID, ForeignKey("provider_snapshots.id", ondelete="CASCADE"), index=True
    )
    client_id: Mapped[str] = mapped_column(GUID, ForeignKey("clients.id", ondelete="CASCADE"))
    provider: Mapped[Provider] = mapped_column(_str_enum(Provider))
    metric_key: Mapped[str] = mapped_column(String(100))
    period_role: Mapped[PeriodRole] = mapped_column(_str_enum(PeriodRole))
    period_start: Mapped[date | None] = mapped_column(Date, default=None)
    period_end: Mapped[date | None] = mapped_column(Date, default=None)
    value_numeric: Mapped[Decimal | None] = mapped_column(DecimalString, default=None)
    value_text: Mapped[str | None] = mapped_column(String(500), default=None)
    unit: Mapped[MetricUnit] = mapped_column(_str_enum(MetricUnit))
    quality: Mapped[Quality] = mapped_column(_str_enum(Quality))
    is_derived: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class Artifact(Base):
    __tablename__ = "artifacts"

    id: Mapped[str] = mapped_column(GUID, primary_key=True, default=new_uuid)
    job_id: Mapped[str] = mapped_column(
        GUID, ForeignKey("sync_jobs.id", ondelete="CASCADE"), index=True
    )
    provider: Mapped[Provider] = mapped_column(_str_enum(Provider))
    kind: Mapped[str] = mapped_column(String(50))
    path: Mapped[str] = mapped_column(String(500))  # relative to the artifacts root, never absolute
    sha256: Mapped[str] = mapped_column(String(64))
    size_bytes: Mapped[int | None] = mapped_column(Integer, default=None)
    mime: Mapped[str | None] = mapped_column(String(100), default=None)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class ImportedSource(Base):
    __tablename__ = "imported_sources"
    __table_args__ = (UniqueConstraint("sha256", name="uq_imported_sources_sha256"),)

    id: Mapped[str] = mapped_column(GUID, primary_key=True, default=new_uuid)
    source_path: Mapped[str] = mapped_column(String(1000))
    sha256: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    imported_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    importer_version: Mapped[str] = mapped_column(String(100))


# --- Added by task-16 (GSC dimensional data, PLAN §21.4) ---------------------
#
# Inherited-zone addition to an otherwise task-03-owned file: task-16's plan
# takes over migration/model ownership for exactly this one table (see its
# "Inherited zone" note) and touches nothing else here. `dimension_value`
# is the GSC URL or search query; `DIMENSION_OTHER_VALUE` below is the
# reserved sentinel for a top-N cut's aggregated remainder row, never a real
# URL/query.


class DimensionKind(StrEnum):
    page = "page"
    query = "query"


class GscDimensionMetric(Base):
    """Per-page/per-query GSC data (PLAN §21.4). Immutable after commit —
    same invariant as `ProviderSnapshot`/`MetricValue` (see the `before_update`
    guard below).

    `quality` is set per-row (not per-snapshot): every row of a period whose
    result was cut to top-N carries `Quality.truncated`, including the
    aggregated remainder row, so a caller reading a handful of rows out of
    context still sees they are not the full set (PLAN §12). `rank` is the
    row's 1-based position in that period's impressions ordering; `None` for
    the remainder row, which stands for many rows, not one.

    One `ProviderSnapshot` holds exactly one period's rows: the unique
    constraint below has no `period_role`/`period_start` component (unlike
    `MetricValue`'s `(snapshot_id, metric_key, period_role)`), so a
    current+previous collection persists as TWO snapshots, one per period —
    never one snapshot double-booked with the same URL/query twice.
    """

    __tablename__ = "gsc_dimension_metrics"
    __table_args__ = (
        UniqueConstraint(
            "snapshot_id",
            "dimension",
            "dimension_value",
            name="uq_gsc_dimension_metrics_snapshot_dimension_value",
        ),
        Index(
            "ix_gsc_dimension_metrics_client_dimension_period",
            "client_id",
            "dimension",
            "period_start",
        ),
        Index(
            "ix_gsc_dimension_metrics_client_dimension_clicks",
            "client_id",
            "dimension",
            "clicks",
        ),
    )

    id: Mapped[str] = mapped_column(GUID, primary_key=True, default=new_uuid)
    snapshot_id: Mapped[str] = mapped_column(
        GUID, ForeignKey("provider_snapshots.id", ondelete="CASCADE"), index=True
    )
    client_id: Mapped[str] = mapped_column(GUID, ForeignKey("clients.id", ondelete="CASCADE"))
    dimension: Mapped[DimensionKind] = mapped_column(_str_enum(DimensionKind, length=16))
    dimension_value: Mapped[str] = mapped_column(String(2048))
    period_start: Mapped[date] = mapped_column(Date)
    period_end: Mapped[date] = mapped_column(Date)
    clicks: Mapped[int | None] = mapped_column(Integer, default=None)
    impressions: Mapped[int | None] = mapped_column(Integer, default=None)
    ctr: Mapped[Decimal | None] = mapped_column(DecimalString, default=None)
    position: Mapped[Decimal | None] = mapped_column(DecimalString, default=None)
    quality: Mapped[Quality] = mapped_column(_str_enum(Quality))
    rank: Mapped[int | None] = mapped_column(Integer, default=None)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


DIMENSION_OTHER_VALUE = "інше"


class AnalyticsDatasetRow(Base):
    """A normalized row from a provider analytical dataset.

    Metric names and payloads differ between GA4, GBP and Ahrefs datasets, so
    provider fields are JSON while the shared query axes stay typed/indexed.
    """

    __tablename__ = "analytics_dataset_rows"
    __table_args__ = (
        UniqueConstraint(
            "snapshot_id",
            "dataset_key",
            "dimension_value",
            name="uq_analytics_dataset_snapshot_value",
        ),
        Index(
            "ix_analytics_dataset_client_key_period",
            "client_id",
            "dataset_key",
            "period_start",
            "period_end",
        ),
    )

    id: Mapped[str] = mapped_column(GUID, primary_key=True, default=new_uuid)
    snapshot_id: Mapped[str] = mapped_column(
        GUID, ForeignKey("provider_snapshots.id", ondelete="CASCADE"), index=True
    )
    client_id: Mapped[str] = mapped_column(
        GUID, ForeignKey("clients.id", ondelete="CASCADE"), index=True
    )
    provider: Mapped[Provider] = mapped_column(_str_enum(Provider), index=True)
    dataset_key: Mapped[str] = mapped_column(String(80))
    dimension_key: Mapped[str] = mapped_column(String(80))
    dimension_value: Mapped[str] = mapped_column(String(2048))
    period_start: Mapped[date] = mapped_column(Date)
    period_end: Mapped[date] = mapped_column(Date)
    metrics_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    quality: Mapped[Quality] = mapped_column(_str_enum(Quality))
    rank: Mapped[int | None] = mapped_column(Integer, default=None)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class ClientHealthDaily(Base):
    """One composite health % per client per day (PLAN §21.2).

    The composite score is derived, not collected: `compute_health()` recomputes
    it from whatever provider snapshots are current, and nothing used to keep
    the result. That left the dashboard card able to show *today's* number but
    no trend at all — neither "which way is this client moving" nor a series to
    chart. This table is that missing history.

    Deliberately NOT immutable, unlike `ProviderSnapshot`/`MetricValue`: a day's
    score is a running summary of that day's best-known data, so a later
    collection on the same day legitimately supersedes it. Hence the unique
    `(client_id, date)` and an upsert on write, rather than an append.

    Only real scores are ever stored — a day with no contributing service is
    absent from the series, never persisted as `0` (PLAN §12, None != 0).
    """

    __tablename__ = "client_health_daily"
    __table_args__ = (
        UniqueConstraint("client_id", "day", name="uq_client_health_daily_client_id_day"),
        Index("ix_client_health_daily_client_day", "client_id", "day"),
    )

    id: Mapped[str] = mapped_column(GUID, primary_key=True, default=new_uuid)
    client_id: Mapped[str] = mapped_column(GUID, ForeignKey("clients.id", ondelete="CASCADE"))
    # `day`, not `date`: a column named `date` would shadow the `date` type in
    # its own `Mapped[date]` annotation.
    day: Mapped[date] = mapped_column(Date)
    value: Mapped[Decimal] = mapped_column(DecimalString)
    coverage_contributing: Mapped[int] = mapped_column(Integer, default=0)
    coverage_configured: Mapped[int] = mapped_column(Integer, default=0)
    computed_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


def _reject_update(mapper: Any, connection: Any, target: Base) -> None:
    raise ValueError(f"{type(target).__name__} rows are immutable after commit")


event.listen(ProviderSnapshot, "before_update", _reject_update)
event.listen(MetricValue, "before_update", _reject_update)
event.listen(GscDimensionMetric, "before_update", _reject_update)
