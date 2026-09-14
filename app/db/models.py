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
    Text,
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
    ClientStatus,
    ComparisonMode,
    JobStatus,
    MetricUnit,
    PeriodKind,
    PeriodRole,
    Provider,
    Quality,
    SourceMode,
    StageStatus,
    UserRole,
)


def _str_enum(enum_cls: type[enum.Enum], length: int = 32) -> SAEnum:
    """SQLite has no native enum type: store `.value` in a CHECK-constrained VARCHAR.

    `native_enum=False` (task-75 §3.2) keeps this the same VARCHAR+CHECK shape
    on Postgres too. Without it, `create_all` on Postgres emits a native
    `CREATE TYPE ... AS ENUM`, and adding one value later becomes an
    `ALTER TYPE ... ADD VALUE` — non-transactional and cannot be used in the
    same transaction that also uses the new value. `Provider` alone has
    grown with nearly every integration task; a plain `CHECK` constraint
    keeps that a normal, revertible migration.
    """
    return SAEnum(
        enum_cls,
        values_callable=lambda obj: [e.value for e in obj],
        length=length,
        native_enum=False,
    )


class Client(Base):
    __tablename__ = "clients"
    __table_args__ = (UniqueConstraint("slug", name="uq_clients_slug"),)

    id: Mapped[str] = mapped_column(GUID, primary_key=True, default=new_uuid)
    name: Mapped[str] = mapped_column(String(200))
    # `display_name` (task-85 §2.1) — the full name shown on CLIENT-facing
    # surfaces only (the report document's `<h1>`). `NULL` = "use `name`":
    # every existing row keeps rendering exactly as before. Internal surfaces
    # (dashboard, filters, `#/trends`) stay on the short `name` on purpose —
    # that code is what the agency itself expects to see there.
    display_name: Mapped[str | None] = mapped_column(String(200), default=None)
    slug: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    domain: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    logo_path: Mapped[str | None] = mapped_column(String(500), default=None)
    market: Mapped[str | None] = mapped_column(String(100), default=None)
    timezone: Mapped[str] = mapped_column(String(64), default="America/New_York")
    # `enabled` — технічна видимість рядка: `False` ховає клієнта з дашборду
    # (`ClientRepository.list_active`). Це НЕ «пауза».
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # `is_dev` — рядок не є реальним (платним) клієнтом (sandbox/dev-домен,
    # тестовий запис). На відміну від `enabled`/`status` нижче — не ховає
    # рядок і не впливає на масові операції, лише позначка «DEV» в UI.
    is_dev: Mapped[bool] = mapped_column(Boolean, default=False)
    # `status` (task-34) — комерційний стан співпраці: `paused` = оплата
    # припинена. Клієнт лишається видимим і зберігає історію, але випадає з
    # масових операцій (`list_collectable`). Індексу немає навмисно: у таблиці
    # десятки рядків, і кожен запит однаково читає її повністю.
    status: Mapped[ClientStatus] = mapped_column(
        _str_enum(ClientStatus, 16), default=ClientStatus.active
    )
    # NULL = статус ніколи не міняли. Дату створення сюди не підставляємо:
    # «Paused since …» має спиратись на реальну подію, а не на здогад.
    status_changed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)
    # Вільна нотатка оператора («payment stopped 01.07»). Не секрет — PLAN §13
    # стосується ключів/паролів/куків, а не комерційного контексту.
    status_note: Mapped[str | None] = mapped_column(String(300), default=None)
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
    # Free-form, human-readable live-progress text (e.g. "found 148 posts so
    # far") — `current_stage` alone is a static per-stage label, not enough to
    # show a running count. NULL for any adapter that never sets it.
    progress_message: Mapped[str | None] = mapped_column(String(200), default=None)
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    lease_owner: Mapped[str | None] = mapped_column(String(200), default=None)
    lease_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)
    heartbeat_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)
    # `(provider, profile_name)` browser/session lock key — PLAN §8.3.
    profile_key: Mapped[str | None] = mapped_column(String(255), default=None)
    # Which parallel lane claimed this cell (task-31), kept after it finishes so
    # the run stays readable after the fact. `None` = never claimed into a
    # numbered lane (still queued, or a single-lane provider). The scheduler
    # never reads it back — capacity is counted from `running` cells per
    # `profile_key` — it exists so progress and logs can name the lane.
    slot: Mapped[int | None] = mapped_column(Integer, default=None)
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


# Той самий cap, що й на клітинку джерела (`app.tools.alt_text.tables`), але
# оголошений тут окремо: `app/db` не має залежати від інструментів.
MAX_ALT_TEXT_LENGTH = 8_192


class AltTextReport(Base):
    """Один завантажений аудит картинок для інструмента Image Alt Text (task-27).

    Чому окремі таблиці, а не наявний `AnalyticsDatasetRow` (як зробили
    task-25/26): той рядок привʼязаний до `ProviderSnapshot`, провайдера й
    періоду — і **immutable**. Тут нема ні провайдера, ні періоду, а рядки
    мутабельні *за задумом*: статус прогресу й результат перевірки
    змінюються щоразу, коли оператор копіює alt або перевіряє сторінку.
    Втискування цього у snapshot-модель або ламало б інваріант
    незмінності, або вимагало append-only-хаків на кожен клік.

    `client_id` опційний: аудит роблять і для проспекта, якого ще немає в
    списку клієнтів. Домен клієнта (коли він є) використовується лише для
    класифікації «свій/чужий хост» у деривації.
    """

    __tablename__ = "alt_text_reports"
    __table_args__ = (
        Index("ix_alt_text_reports_source_sha256", "source_sha256"),
        Index("ix_alt_text_reports_created_at", "created_at"),
    )

    id: Mapped[str] = mapped_column(GUID, primary_key=True, default=new_uuid)
    client_id: Mapped[str | None] = mapped_column(
        GUID, ForeignKey("clients.id", ondelete="SET NULL"), default=None
    )
    title: Mapped[str] = mapped_column(String(200))
    source_filename: Mapped[str] = mapped_column(String(255))
    # `String`, не `sa.Enum`: це формат завантаженого файлу для показу, а не
    # спільний контракт провайдерів; валідація живе у Pydantic-DTO.
    source_kind: Mapped[str] = mapped_column(String(16))
    source_sheet: Mapped[str | None] = mapped_column(String(255), default=None)
    # Ідемпотентність: повторне завантаження того самого файлу+вкладки віддає
    # 409 з наявним звітом замість тихого дубліката.
    source_sha256: Mapped[str] = mapped_column(String(64))
    column_map_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    row_count: Mapped[int] = mapped_column(Integer, default=0)
    # Рядки джерела без URL картинки: показуємо число, а не ховаємо втрату.
    skipped_count: Mapped[int] = mapped_column(Integer, default=0)
    warnings_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


class AltTextItem(Base):
    """Одна картинка звіту: дериваційний результат + прогрес + перевірка.

    Мутабельний рядок (див. `AltTextReport`). `status` веде прогрес оператора,
    `verify_*` — окремий факт про сторінку. Вони НЕ одне й те саме: «alt
    скопійовано» ≠ «alt стоїть на сторінці», тому статус ніколи не виводиться
    з вердикту, окрім явного `match`.
    """

    __tablename__ = "alt_text_items"
    __table_args__ = (
        UniqueConstraint("report_id", "row_index", name="uq_alt_text_items_report_id_row_index"),
        Index("ix_alt_text_items_report_status", "report_id", "status"),
    )

    id: Mapped[str] = mapped_column(GUID, primary_key=True, default=new_uuid)
    report_id: Mapped[str] = mapped_column(
        GUID, ForeignKey("alt_text_reports.id", ondelete="CASCADE"), index=True
    )
    # Номер рядка в джерелі (1-based, як його бачить оператор у таблиці).
    row_index: Mapped[int] = mapped_column(Integer)
    image_url: Mapped[str] = mapped_column(String(2048))
    search_key: Mapped[str] = mapped_column(String(512))
    search_key_fallback: Mapped[str | None] = mapped_column(String(512), default=None)
    url_kind: Mapped[str] = mapped_column(String(24), default="unknown")
    # Що саме відрізали від назви файлу + попередження деривації: оператор
    # мусить бачити логіку, а не «магічний» рядок.
    derivation_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    new_alt: Mapped[str] = mapped_column(String(MAX_ALT_TEXT_LENGTH), default="")
    old_alt: Mapped[str | None] = mapped_column(String(MAX_ALT_TEXT_LENGTH), default=None)
    # Первинна сторінка (перша зі списку) — лишається для простих читань.
    page_url: Mapped[str | None] = mapped_column(String(2048), default=None)
    # Одна картинка може стояти на кількох сторінках, і аудити пакують їх усі
    # в одну клітинку. Тримаємо повний список, бо перевірка alt має право
    # обійти кілька сторінок, а оператор — відкрити будь-яку.
    page_urls_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    extra_json: Mapped[dict[str, str]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    status_changed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)
    verify_status: Mapped[str | None] = mapped_column(String(24), default=None)
    verify_alt: Mapped[str | None] = mapped_column(String(MAX_ALT_TEXT_LENGTH), default=None)
    verify_http_status: Mapped[int | None] = mapped_column(Integer, default=None)
    verify_detail: Mapped[str | None] = mapped_column(String(500), default=None)
    verify_checked_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)
    # WordPress `attachment ID` (task-39): точний збіг у медіатеці, не «схожий
    # файл». `NULL` = ще не шукали — не плутати з `not_found` (шукали, нема).
    wp_media_id: Mapped[int | None] = mapped_column(Integer, default=None)
    wp_media_status: Mapped[str | None] = mapped_column(String(24), default=None)
    wp_media_confidence: Mapped[str | None] = mapped_column(String(16), default=None)
    # Alt, що ЗАРАЗ стоїть у медіатеці WordPress — не плутати з `verify_alt`
    # (alt, знайдений на сторінці): це різні джерела істини.
    wp_media_alt: Mapped[str | None] = mapped_column(String(MAX_ALT_TEXT_LENGTH), default=None)
    wp_media_admin_url: Mapped[str | None] = mapped_column(String(2048), default=None)
    wp_media_detail: Mapped[str | None] = mapped_column(String(500), default=None)
    # Кандидати при `ambiguous` — вибір лишається за оператором, не вгадуємо.
    wp_media_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    wp_media_checked_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)


class ContentQaReport(Base):
    """Один прогін Strategy Check / Framework Check (task-53).

    Одна таблиця на обидва тули — `doc_type` розрізняє: `findings_json` не має
    per-finding мутабельного стану (на відміну від `AltTextItem.status`), тож
    дитячої таблиці не потрібно, весь звіт — один незмінний рядок (без
    `updated_at`: звіт не редагується, повторний прогін створює новий рядок).

    `client_id` опційний тим самим міркуванням, що в `AltTextReport`: перевірку
    роблять і для проспекта, якого ще нема в списку клієнтів.
    """

    __tablename__ = "content_qa_reports"
    __table_args__ = (
        Index("ix_content_qa_reports_doc_type_created_at", "doc_type", "created_at"),
    )

    id: Mapped[str] = mapped_column(GUID, primary_key=True, default=new_uuid)
    client_id: Mapped[str | None] = mapped_column(
        GUID, ForeignKey("clients.id", ondelete="SET NULL"), default=None
    )
    # "strategy_xlsx" | "framework_docx" — той самий discriminator, що
    # `ContentDoc.doc_type` в app/contentqa/model.py. String, не sa.Enum: це
    # UI/каталожний вибір інструмента, не спільний контракт провайдерів.
    doc_type: Mapped[str] = mapped_column(String(32))
    source_filename: Mapped[str] = mapped_column(String(255))
    # kind/intent/locale/brand_names — усе, що оператор задав перед прогоном.
    profile_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    item_count: Mapped[int] = mapped_column(Integer, default=0)
    skipped_sheets_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    findings_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    severity_counts_json: Mapped[dict[str, int]] = mapped_column(JSON, default=dict)
    # Стан кожного аркуша workbook (task-87 §1.3): checked/not_selected/
    # no_recognized_columns + item_count. `[]` для рядків до task-87
    # (nullable, backfilled лише лінивою десеріалізацією в DTO — `[]`
    # читається так само, як і "не було колонки") і для DOCX-звітів
    # (Framework Check аркушів не має).
    sheets_json: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class ContentQaPackage(Base):
    """Один прогін Strategy Package Check (task-90): одна стратегія + N
    фреймворків.

    **Один рядок на весь пакет**, не N дочірніх `ContentQaReport` (свідоме
    відхилення від першого чорновика цієї моделі): звіт пакета (task-90 §7)
    показує вкладки Frame/Copy/Cross-checks **зведено по всіх фреймворках
    одразу**, а не навігацію "один документ = одна сторінка" — тобто
    фронту й так треба прочитати ВСІ фреймворки одним запитом, і дочірні
    рядки дали б лише зайвий JOIN заради структури, якою ніхто не користується.
    `frameworks_json` — список повних `FrameworkOutcome` (стан, findings
    Frame+Copy+три звірки, назви вкладок) по кожному кандидату; `.ref`
    усередині кожного finding уже несе назву документа, тож ідентифікація
    не губиться.

    `strategy_findings_json`/`aggregate_findings_json` — те, що не належить
    жодному одному фреймворку: сама QA стратегії і крос-фреймворковий
    `sync-strategy-row-no-frame` (task-90 §2.4). Immutable рядок, як і
    `ContentQaReport`: повторний прогін створює новий пакет, не оновлює
    старий.
    """

    __tablename__ = "content_qa_packages"
    __table_args__ = (
        Index("ix_content_qa_packages_created_at", "created_at"),
        Index(
            "ix_content_qa_packages_created_by_user_id_created_at",
            "created_by_user_id",
            "created_at",
        ),
    )

    id: Mapped[str] = mapped_column(GUID, primary_key=True, default=new_uuid)
    client_id: Mapped[str | None] = mapped_column(
        GUID, ForeignKey("clients.id", ondelete="SET NULL"), default=None
    )
    # "gsheet" | "xlsx_blob" — звідки прочитана сама стратегія (task-90 §5,
    # `DriveFile.mime_type` розводить на кроці /resolve). Не плутати з
    # `ContentQaReport.doc_type`, який розрізняє СТРАТЕГІЮ/ФРЕЙМВОРК/КОПІЮ.
    source_kind: Mapped[str] = mapped_column(String(16))
    source_file_id: Mapped[str | None] = mapped_column(String(128), default=None)
    source_name: Mapped[str] = mapped_column(String(255))
    profile_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    framework_count: Mapped[int] = mapped_column(Integer, default=0)
    strategy_findings_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    # `sync-strategy-row-no-frame` та подібне — агрегатні знахідки, що
    # бачать усі фреймворки пакета одразу (task-90 §2.4), тож не належать
    # жодному одному `ContentQaReport`.
    aggregate_findings_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    # Стан кожного кандидата-фреймворка (checked/not_accessible/
    # no_recognized_tabs), звідки він узятий (strategy_link/drive_search) і
    # зведені CrossCheckInfo трьох лейнів — усе, що потрібно блоку Scope у
    # звіті, не перелазячи по N окремих `ContentQaReport` (task-90 §7).
    frameworks_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    severity_counts_json: Mapped[dict[str, int]] = mapped_column(JSON, default=dict)
    # Стан кожного аркуша стратегії (checked/not_selected/no_recognized_columns
    # + item_count) — та сама трійка станів, що `ContentQaReport.sheets_json`
    # (task-87 §1.3), для блоку Scope у звіті пакета.
    sheets_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    # Матриця Стратегія/Фреймворк/Сайт/Вердикт (task-91 §2.2) — список
    # `MatrixRowDTO`. Nullable, той самий патерн, що `ContentQaReport.
    # sheets_json` (task-87): `NULL` на пакетах, прогнаних до task-91 —
    # API читає це як `[]` (`app/api/tools_strategy_package.py`), фронт
    # показує чесний "ця версія звіту ще не має порівняльної таблиці", не
    # вгадує заднім числом (CLAUDE.md).
    matrix_json: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    # Хто прогнав пакет (task-93) — той самий словник/`SET NULL`-міркування,
    # що в `ReportRun`/`ReportBatch`.
    created_by_user_id: Mapped[str | None] = mapped_column(
        GUID, ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    created_by_kind: Mapped[str | None] = mapped_column(String(16), default=None)


class RedirectCheck(Base):
    """Один прогін інструмента Redirect Checker (task-29).

    Той самий вибір, що в `AltTextReport`: мутабельні таблиці інструмента, а не
    immutable `AnalyticsDatasetRow` — рядок перезаписується при повторному
    трейсі, і провайдера/періоду тут немає.

    `client_id` опційний: ланцюжки перевіряють і для проспекта, і «просто так»
    (розбір чужого backlink-а).
    """

    __tablename__ = "redirect_checks"
    __table_args__ = (Index("ix_redirect_checks_created_at", "created_at"),)

    id: Mapped[str] = mapped_column(GUID, primary_key=True, default=new_uuid)
    client_id: Mapped[str | None] = mapped_column(
        GUID, ForeignKey("clients.id", ondelete="SET NULL"), default=None
    )
    title: Mapped[str] = mapped_column(String(200))
    # Скільки URL прислали / скільки лишилось після нормалізації й дедуплікації.
    # Різниця видима в UI: рядки не зникають молча.
    input_count: Mapped[int] = mapped_column(Integer, default=0)
    target_count: Mapped[int] = mapped_column(Integer, default=0)
    # Скільки прибрала дедуплікація і чи вдарився список у cap — окремі числа,
    # бо «прислали 250, перевіряємо 200» оператор має бачити, а не вгадувати.
    duplicate_count: Mapped[int] = mapped_column(Integer, default=0)
    truncated: Mapped[bool] = mapped_column(Boolean, default=False)
    # З якими опціями прогін зроблено (max_hops, follow_meta_refresh, cache_buster):
    # без цього результат неможливо трактувати через тиждень.
    options_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # Рядки, які посиланням не є (`mailto:`, сміття) — з причиною, а не тихо.
    rejected_json: Mapped[list[dict[str, str]]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


class RedirectCheckTarget(Base):
    """Один вхідний URL прогону: ланцюжок + вердикт + прапорці.

    Хопи лежать у `chain_json`, а не в третій таблиці: ланцюжок завжди читається
    цілком (ніколи не фільтрується й не агрегується по хопах), тож JSON дає
    атомарний запис «як воно було» одним UPDATE-ом і не плодить рядків, які
    ніхто не питає окремо.

    `checked_at is None` означає **«не перевіряли»** — і це не те саме, що
    «редіректів немає» (PLAN §12).
    """

    __tablename__ = "redirect_check_targets"
    __table_args__ = (
        UniqueConstraint(
            "check_id", "row_index", name="uq_redirect_check_targets_check_id_row_index"
        ),
        Index("ix_redirect_check_targets_check_status", "check_id", "run_status"),
        Index("ix_redirect_check_targets_check_verdict", "check_id", "verdict"),
    )

    id: Mapped[str] = mapped_column(GUID, primary_key=True, default=new_uuid)
    check_id: Mapped[str] = mapped_column(
        GUID, ForeignKey("redirect_checks.id", ondelete="CASCADE"), index=True
    )
    row_index: Mapped[int] = mapped_column(Integer)
    # Як вставили (traceability) і що реально запитували.
    input_url: Mapped[str] = mapped_column(String(2048))
    normalized_url: Mapped[str] = mapped_column(String(2048))
    input_notes_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    run_status: Mapped[str] = mapped_column(String(12), default="queued")
    verdict: Mapped[str | None] = mapped_column(String(24), default=None)
    final_url: Mapped[str | None] = mapped_column(String(2048), default=None)
    final_status_code: Mapped[int | None] = mapped_column(Integer, default=None)
    # Кількість **редіректів** (0 = прямий 200), а не довжина списку хопів.
    hop_count: Mapped[int | None] = mapped_column(Integer, default=None)
    chain_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    flags_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    final_page_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    error_code: Mapped[str | None] = mapped_column(String(32), default=None)
    # Причина без секретів: URL можуть містити токени, тож тут лише хост+причина.
    error_detail: Mapped[str | None] = mapped_column(String(1000), default=None)
    elapsed_ms: Mapped[int | None] = mapped_column(Integer, default=None)
    checked_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)


# --- Added by task-35 (Site Monitor, PLAN §16 "Етап 8") ---------------------
#
# "Current state + immutable version chain" (task-35 §3.2): a mutable row per
# object carries `last_checked_at` and today's values; a version row is
# appended ONLY when the fingerprint actually changed, so storage grows with
# real change, not with run count. `kind`/`status`/`severity`/`scope`/
# `change_type` are plain `String`, not `sa.Enum` — the same choice already
# made for `redirect_checks`/`alt_text_items`: these are tool-level states
# defined in `app/monitor/severity.py`, and adding one must not require DDL.


class MonitorSettings(Base):
    """Per-client monitor configuration. Lazily created on first touch —
    `MonitorRepository.get_or_create_settings` — so a client with no row yet
    still reads as "monitoring enabled with the global defaults" rather than
    needing a migration-time backfill for every existing client.
    """

    __tablename__ = "monitor_settings"
    __table_args__ = (UniqueConstraint("client_id", name="uq_monitor_settings_client_id"),)

    id: Mapped[str] = mapped_column(GUID, primary_key=True, default=new_uuid)
    client_id: Mapped[str] = mapped_column(GUID, ForeignKey("clients.id", ondelete="CASCADE"))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    base_url_override: Mapped[str | None] = mapped_column(String(500), default=None)
    # NULL = not known which host the site canonically redirects to (`www.`
    # vs bare) — `_base_url` uses `client.domain` verbatim, exactly as before
    # this field existed. `True`/`False` picks the host directly instead of
    # relying on the site's own redirect to correct it on every request
    # (task-38 follow-up — operator-confused-by-redirect-noise report).
    canonical_www: Mapped[bool | None] = mapped_column(Boolean, default=None)
    # NULL = use the global `monitor_*_interval_hours` setting.
    light_interval_hours: Mapped[float | None] = mapped_column(Float, default=None)
    full_interval_hours: Mapped[float | None] = mapped_column(Float, default=None)
    max_urls: Mapped[int | None] = mapped_column(Integer, default=None)
    # A paused client (task-34) drops out of the schedule by default; this is
    # the per-client toggle that opts a specific paused client back in
    # (confirmed with the user 2026-08-11).
    monitor_when_paused: Mapped[bool] = mapped_column(Boolean, default=False)
    ignore_patterns_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    muted_change_types_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    # Antibot lane (task-50): "auto" | "http" | "browser". "http"/"browser"
    # are an explicit operator choice and always win (`http` doubles as the
    # emergency kill switch — §B6); "auto" starts cheap and self-escalates
    # once a run confirms a block (`blocked_detected_at` below), see
    # `app.monitor.runner.effective_fetch_mode`.
    fetch_mode: Mapped[str] = mapped_column(String(16), default="auto")
    # Set by the runner the moment a run confirms >=1 blocked page while
    # `fetch_mode == "auto"`; read back by `effective_fetch_mode` to decide
    # whether the NEXT run still needs the slow lane (task-50 §B5). `NULL` =
    # never confirmed blocked, or an explicit operator mode overrides it.
    blocked_detected_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)
    # Redirect monitoring (task-51). `redirect_source`: "auto" | "redirection"
    # | "probe" | "off" — "auto" caches its `detect_source()` verdict in
    # `redirect_source_json` (`{"source": ..., "checked_at": ...}`) so every
    # light run does not re-probe `/wp-json/` for nothing.
    redirects_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    redirect_source: Mapped[str] = mapped_column(String(16), default="auto")
    redirect_source_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # `NULL` = redirects have never been successfully collected for this
    # client — the baseline gate AND the lower bound of "appeared between X
    # and Y" for the very first rules ever seen (task-51 §3.5): distinct from
    # the run's own `is_baseline_run` because an operator can add Application
    # Password credentials months into monitoring an existing client.
    redirects_last_checked_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


class MonitorRun(Base):
    """One scheduler tick's (or manual "Check now") pass over one client.

    Mutable while `status == "running"` (counters are updated as the run
    progresses so a poller can show live progress — same pattern as
    `SyncJob.progress_percent`), then left alone once finished.
    """

    __tablename__ = "monitor_runs"
    __table_args__ = (Index("ix_monitor_runs_client_started", "client_id", "started_at"),)

    id: Mapped[str] = mapped_column(GUID, primary_key=True, default=new_uuid)
    client_id: Mapped[str] = mapped_column(
        GUID, ForeignKey("clients.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(16))  # "light" | "full"
    trigger: Mapped[str] = mapped_column(String(16), default="scheduled")  # "scheduled" | "manual"
    status: Mapped[str] = mapped_column(String(24), default="running", index=True)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)
    # `NULL` for rows written before this column existed — `None` != `0`
    # (project invariant): an old row never renders as "Checked N of 0".
    urls_total: Mapped[int | None] = mapped_column(Integer, default=None)
    urls_checked: Mapped[int] = mapped_column(Integer, default=0)
    urls_changed: Mapped[int] = mapped_column(Integer, default=0)
    urls_new: Mapped[int] = mapped_column(Integer, default=0)
    urls_gone: Mapped[int] = mapped_column(Integer, default=0)
    requests_made: Mapped[int] = mapped_column(Integer, default=0)
    bytes_downloaded: Mapped[int] = mapped_column(Integer, default=0)
    changes_critical: Mapped[int] = mapped_column(Integer, default=0)
    changes_warning: Mapped[int] = mapped_column(Integer, default=0)
    changes_info: Mapped[int] = mapped_column(Integer, default=0)
    # Honest "checked N of M" instead of silently starting over next time
    # (task-35 §3.3 ring traversal) — `remaining_count` is what is left.
    truncated: Mapped[bool] = mapped_column(Boolean, default=False)
    remaining_count: Mapped[int | None] = mapped_column(Integer, default=None)
    error_code: Mapped[str | None] = mapped_column(String(100), default=None)
    error_message: Mapped[str | None] = mapped_column(String(1000), default=None)
    # e.g. `{"baseline": true}` — read by the severity engine to suppress
    # alerts on a client's very first run (task-35 §3.4 "перший прогін...").
    notes_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class MonitorBlob(Base):
    """Content-addressed store for fetched documents (robots.txt/sitemap/page
    bodies). `sha256` is the primary key — identical bytes from any client on
    any run are stored exactly once (task-35 §3.2.1).
    """

    __tablename__ = "monitor_blobs"

    sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    size_bytes: Mapped[int] = mapped_column(Integer)
    gzip_size_bytes: Mapped[int] = mapped_column(Integer)
    mime: Mapped[str | None] = mapped_column(String(100), default=None)
    # Relative to the artifacts root, never absolute (mirrors `Artifact.path`).
    path: Mapped[str] = mapped_column(String(500))
    first_seen_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class MonitorResource(Base):
    """Current state of robots.txt or one sitemap file (root or a child of a
    sitemap index) — one row per FILE, not per URL entry inside it.
    """

    __tablename__ = "monitor_resources"
    __table_args__ = (
        UniqueConstraint("client_id", "url", name="uq_monitor_resources_client_id_url"),
    )

    id: Mapped[str] = mapped_column(GUID, primary_key=True, default=new_uuid)
    client_id: Mapped[str] = mapped_column(
        GUID, ForeignKey("clients.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(16))  # "robots" | "sitemap"
    url: Mapped[str] = mapped_column(String(2048))
    status: Mapped[int | None] = mapped_column(Integer, default=None)
    content_sha256: Mapped[str | None] = mapped_column(String(64), default=None)
    # Parsed structure: robots' UA-group -> rules, or a sitemap file's
    # url_count + child sitemap URLs (index files) — never the full per-URL
    # list of a urlset, which lives via `MonitorPage.in_sitemap` instead.
    structure_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    version_seq: Mapped[int] = mapped_column(Integer, default=0)
    missing_since: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)
    last_checked_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class MonitorResourceVersion(Base):
    """Immutable after commit — see the `before_update` guard below."""

    __tablename__ = "monitor_resource_versions"
    __table_args__ = (
        Index("ix_monitor_resource_versions_resource_observed", "resource_id", "observed_at"),
    )

    id: Mapped[str] = mapped_column(GUID, primary_key=True, default=new_uuid)
    resource_id: Mapped[str] = mapped_column(
        GUID, ForeignKey("monitor_resources.id", ondelete="CASCADE"), index=True
    )
    client_id: Mapped[str] = mapped_column(GUID, ForeignKey("clients.id", ondelete="CASCADE"))
    observed_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    http_status: Mapped[int | None] = mapped_column(Integer, default=None)
    content_sha256: Mapped[str | None] = mapped_column(String(64), default=None)
    structure_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    changed_fields_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    prev_version_id: Mapped[str | None] = mapped_column(
        GUID, ForeignKey("monitor_resource_versions.id", ondelete="SET NULL"), default=None
    )
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class MonitorPage(Base):
    """Current state of one URL (task-35 §3.3)."""

    __tablename__ = "monitor_pages"
    __table_args__ = (
        UniqueConstraint("client_id", "url", name="uq_monitor_pages_client_id_url"),
        Index("ix_monitor_pages_client_last_checked", "client_id", "last_checked_at"),
    )

    id: Mapped[str] = mapped_column(GUID, primary_key=True, default=new_uuid)
    client_id: Mapped[str] = mapped_column(
        GUID, ForeignKey("clients.id", ondelete="CASCADE"), index=True
    )
    url: Mapped[str] = mapped_column(String(2048))
    is_key_page: Mapped[bool] = mapped_column(Boolean, default=False)
    in_sitemap: Mapped[bool] = mapped_column(Boolean, default=True)
    status: Mapped[int | None] = mapped_column(Integer, default=None)
    final_url: Mapped[str | None] = mapped_column(String(2048), default=None)
    hop_count: Mapped[int | None] = mapped_column(Integer, default=None)
    # `None` = not checked yet — NOT the same as "not indexable" (PLAN §12).
    indexable: Mapped[bool | None] = mapped_column(Boolean, default=None)
    meta_robots: Mapped[str | None] = mapped_column(String(200), default=None)
    x_robots_tag: Mapped[str | None] = mapped_column(String(200), default=None)
    canonical: Mapped[str | None] = mapped_column(String(2048), default=None)
    title: Mapped[str | None] = mapped_column(String(500), default=None)
    # task-75 §3.1: was `String(1000)`. Live production data has meta
    # descriptions up to 8385 chars (SQLite never enforced the declared
    # VARCHAR length, so this went unnoticed until a Postgres migration
    # dry-run rejected them with `StringDataRightTruncation`). `Text` has
    # no natural cap and costs nothing extra on Postgres over `varchar`.
    description: Mapped[str | None] = mapped_column(Text, default=None)
    h1: Mapped[str | None] = mapped_column(String(500), default=None)
    word_count: Mapped[int | None] = mapped_column(Integer, default=None)
    analytics_ids_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    signals_sha256: Mapped[str | None] = mapped_column(String(64), default=None)
    text_sha256: Mapped[str | None] = mapped_column(String(64), default=None)
    raw_sha256: Mapped[str | None] = mapped_column(String(64), default=None)
    version_seq: Mapped[int] = mapped_column(Integer, default=0)
    # Set the run a URL first drops out of the sitemap; cleared if it
    # reappears. Still checked for `monitor_gone_retention_days` after this is
    # set, so a real removal is still caught as `page_gone` (task-35 §3.3).
    gone_since: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    last_checked_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


class MonitorPageVersion(Base):
    """Immutable after commit — see the `before_update` guard below.

    `body_blob_sha256` is `None` whenever this version's fingerprint did not
    change from the previous one (task-35 §3.2.1): the raw bytes may still
    have differed (e.g. a per-request Cloudflare nonce), but nothing a human
    would call "the page changed" did, so no new body copy is kept — the
    version still records `raw_sha256` as proof that the bytes differed.
    """

    __tablename__ = "monitor_page_versions"
    __table_args__ = (
        Index("ix_monitor_page_versions_page_observed", "page_id", "observed_at"),
    )

    id: Mapped[str] = mapped_column(GUID, primary_key=True, default=new_uuid)
    page_id: Mapped[str] = mapped_column(
        GUID, ForeignKey("monitor_pages.id", ondelete="CASCADE"), index=True
    )
    client_id: Mapped[str] = mapped_column(GUID, ForeignKey("clients.id", ondelete="CASCADE"))
    observed_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    http_status: Mapped[int | None] = mapped_column(Integer, default=None)
    signals_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    signals_sha256: Mapped[str | None] = mapped_column(String(64), default=None)
    text_sha256: Mapped[str | None] = mapped_column(String(64), default=None)
    raw_sha256: Mapped[str | None] = mapped_column(String(64), default=None)
    body_blob_sha256: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("monitor_blobs.sha256", ondelete="SET NULL"), default=None
    )
    changed_fields_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    prev_version_id: Mapped[str | None] = mapped_column(
        GUID, ForeignKey("monitor_page_versions.id", ondelete="SET NULL"), default=None
    )
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class MonitorDnsRecord(Base):
    """Current state of one `(client, name, type)` DNS record set."""

    __tablename__ = "monitor_dns_records"
    __table_args__ = (
        UniqueConstraint(
            "client_id", "name", "record_type", name="uq_monitor_dns_records_client_name_type"
        ),
    )

    id: Mapped[str] = mapped_column(GUID, primary_key=True, default=new_uuid)
    client_id: Mapped[str] = mapped_column(
        GUID, ForeignKey("clients.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(255))
    record_type: Mapped[str] = mapped_column(String(16))  # A/AAAA/CNAME/NS/MX/TXT/CAA
    values_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    ttl: Mapped[int | None] = mapped_column(Integer, default=None)
    resolver: Mapped[str | None] = mapped_column(String(100), default=None)
    version_seq: Mapped[int] = mapped_column(Integer, default=0)
    last_checked_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


class MonitorDnsVersion(Base):
    """Immutable after commit — see the `before_update` guard below."""

    __tablename__ = "monitor_dns_versions"
    __table_args__ = (Index("ix_monitor_dns_versions_record_observed", "record_id", "observed_at"),)

    id: Mapped[str] = mapped_column(GUID, primary_key=True, default=new_uuid)
    record_id: Mapped[str] = mapped_column(
        GUID, ForeignKey("monitor_dns_records.id", ondelete="CASCADE"), index=True
    )
    client_id: Mapped[str] = mapped_column(GUID, ForeignKey("clients.id", ondelete="CASCADE"))
    observed_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    values_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    ttl: Mapped[int | None] = mapped_column(Integer, default=None)
    resolver: Mapped[str | None] = mapped_column(String(100), default=None)
    prev_version_id: Mapped[str | None] = mapped_column(
        GUID, ForeignKey("monitor_dns_versions.id", ondelete="SET NULL"), default=None
    )
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class MonitorChange(Base):
    """The change/alert feed (task-35 §3.4). Mutable only for ack/resolve.

    Deliberately does NOT use the word "alerts" anywhere (name or field) —
    `/clients/{id}/alerts` already means GSC traffic-drop alerts (task-17);
    reusing the word for a different concept would break "one name, one
    thing everywhere" (CLAUDE.md). `related_id` is a plain, non-FK pointer
    resolved via `scope` (page/resource/dns id) — a change can originate from
    any one of three different tables, and a single polymorphic FK is not
    worth the complexity for a field that is purely a "show me more" link.
    """

    __tablename__ = "monitor_changes"
    __table_args__ = (
        Index("ix_monitor_changes_client_severity_state", "client_id", "severity", "resolved_at"),
        Index("ix_monitor_changes_client_detected", "client_id", "detected_at"),
    )

    id: Mapped[str] = mapped_column(GUID, primary_key=True, default=new_uuid)
    client_id: Mapped[str] = mapped_column(
        GUID, ForeignKey("clients.id", ondelete="CASCADE"), index=True
    )
    run_id: Mapped[str | None] = mapped_column(
        GUID, ForeignKey("monitor_runs.id", ondelete="SET NULL"), default=None
    )
    severity: Mapped[str] = mapped_column(String(16))  # "critical" | "warning" | "info"
    change_type: Mapped[str] = mapped_column(String(64))  # stable key, see app/monitor/severity.py
    scope: Mapped[str] = mapped_column(String(16))  # "site" | "page" | "dns" | "robots" | "sitemap"
    subject: Mapped[str] = mapped_column(String(2048))  # URL, DNS record name, or "robots.txt"
    related_id: Mapped[str | None] = mapped_column(String(64), default=None)
    old_value: Mapped[str | None] = mapped_column(String(2000), default=None)
    new_value: Mapped[str | None] = mapped_column(String(2000), default=None)
    # Aggregates (e.g. `sitemap_broken_urls`) carry their breakdown + example
    # URLs here instead of one row per broken URL (task-35 §3.4.1).
    details_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    detected_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    acknowledged_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)
    acknowledged_note: Mapped[str | None] = mapped_column(String(500), default=None)
    # Set when the underlying condition self-heals (task-35 §3.4 "Самозагасання").
    resolved_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)


# --- Added by task-51 (Redirect monitoring, PLAN §16 "Хвиля M" continued) --
#
# Current state per redirect rule — no separate version-chain table (unlike
# pages/DNS/robots): the WHOLE-list history already lives for free in
# `MonitorResource(kind="redirects")` + `MonitorResourceVersion` (same
# content-addressed blob mechanism robots.txt/sitemaps use), and a per-rule
# CHANGE already carries old_value/new_value on `MonitorChange` the moment it
# is detected — a third, fully parallel version chain would duplicate both
# without adding anything the operator asked for (task-51 §3.1).


class MonitorRedirect(Base):
    """One redirect rule's current state, from either source (task-51 §3.1).

    `first_seen_at`/`first_seen_window_start` are the whole point of this
    table: the plugin itself stores neither a creation date nor an author
    (task-51 §1.3) — our own hourly check is the only thing that can bound
    when a rule appeared, and `first_seen_window_start` is exactly that lower
    bound (the previous successful check for this client+source, `NULL` when
    unknown — PLAN §12, never a guessed date).
    """

    __tablename__ = "monitor_redirects"
    __table_args__ = (
        UniqueConstraint(
            "client_id", "source", "rule_key", name="uq_monitor_redirects_client_source_key"
        ),
    )

    id: Mapped[str] = mapped_column(GUID, primary_key=True, default=new_uuid)
    client_id: Mapped[str] = mapped_column(
        GUID, ForeignKey("clients.id", ondelete="CASCADE"), index=True
    )
    source: Mapped[str] = mapped_column(String(16))  # "redirection" | "probe"
    # `str(id)` (plugin) or the normalized source URL (probe, which has no
    # id of its own) — identity WITHIN one source; the two sources' key
    # spaces are never compared to each other (task-51 contract.py).
    rule_key: Mapped[str] = mapped_column(String(64))
    external_id: Mapped[int | None] = mapped_column(Integer, default=None)
    source_url: Mapped[str] = mapped_column(String(2048))
    target_url: Mapped[str | None] = mapped_column(String(2048), default=None)
    http_code: Mapped[int | None] = mapped_column(Integer, default=None)
    match_type: Mapped[str] = mapped_column(String(32), default="url")
    regex: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(16), default="enabled")
    group_name: Mapped[str | None] = mapped_column(String(255), default=None)
    title: Mapped[str | None] = mapped_column(String(500), default=None)
    rule_sha256: Mapped[str] = mapped_column(String(64))
    first_seen_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    first_seen_window_start: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)
    last_seen_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    # `NULL` = still present as of the latest successful check. Set the run a
    # rule first drops out of the source, cleared if it reappears (mirrors
    # `MonitorPage.gone_since`).
    removed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)
    version_seq: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


# --- Added by task-42 (Reports engine, PLAN §16 "Хвиля N") -----------------
#
# `kind`/`source`/`status`/`cross_check_status` are plain `String`, not
# `sa.Enum` — the same choice task-35 already made for monitor's own
# tool-level states: a new report kind or source needs no enum/CHECK DDL. It
# can still need a width bump: `source` was `String(8)` for `api`/`pdf` and
# task-84's `google_api` (10 chars) needed a widening migration to `String(16)`
# (`b7d3f9a24e18`). Reports are deliberately NOT a `SyncJob`/provider (task-42
# §2.2): no `(client, provider)` cell, no metrics catalog, no browser profile
# lock — just a client, a parameter set and a list of output files.


# --- Added by task-47 (batch reports + client document/PDF) ----------------
#
# A batch is a parent AGGREGATE, not a new kind of work (task-47 §2.2): every
# client in it is a perfectly ordinary `ReportRun` with `batch_id` set, so it
# keeps every existing behaviour for free — periods, `periods_json`, events,
# XLSX, cancellation, stale-run recovery. `client_count`/`clients_done`/
# `clients_failed` are a live-updated summary the scheduler's own tick
# maintains (`app/reports/batch.py`), not something the API recomputes by
# scanning every child run on each read.


class ReportBatch(Base):
    __tablename__ = "report_batches"
    __table_args__ = (Index("ix_report_batches_status", "status"),)

    id: Mapped[str] = mapped_column(GUID, primary_key=True, default=new_uuid)
    kind: Mapped[str] = mapped_column(String(32))
    source: Mapped[str] = mapped_column(String(16))  # api|pdf|google_api (task-84)
    params_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(16), default="queued")
    client_count: Mapped[int] = mapped_column(Integer, default=0)
    clients_done: Mapped[int] = mapped_column(Integer, default=0)
    clients_failed: Mapped[int] = mapped_column(Integer, default=0)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    queued_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    # Хто запустив батч (task-93). `SET NULL`, не `CASCADE`: деактивація чи
    # видалення акаунта людини не має стирати факт "хтось це запустив" —
    # `created_by_kind` лишається (він не FK), лише `created_by_user_id`
    # обнуляється. `NULL` в обох = невідомо (рядок до task-93 або
    # неоднозначний бекфіл) — НІКОЛИ не підміняється на `"system"`.
    created_by_user_id: Mapped[str | None] = mapped_column(
        GUID, ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    # `user | agent | local | cli | system | NULL` — той самий словник, що й
    # `AuditEvent.actor_kind` (task-72).
    created_by_kind: Mapped[str | None] = mapped_column(String(16), default=None)


class ReportRun(Base):
    """One report generation run (task-42 §4). Mutable while `status` is
    `queued`/`running` (counters update live, mirrors `SyncJob.progress_percent`
    and `MonitorRun`), then left alone once finished.
    """

    __tablename__ = "report_runs"
    __table_args__ = (
        Index("ix_report_runs_client_created", "client_id", "created_at"),
        Index("ix_report_runs_status", "status"),
        Index("ix_report_runs_created_by_user_id_created_at", "created_by_user_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(GUID, primary_key=True, default=new_uuid)
    client_id: Mapped[str] = mapped_column(
        GUID, ForeignKey("clients.id", ondelete="CASCADE"), index=True
    )
    # NULL for a standalone run (task-47 §4) — `SET NULL` rather than
    # `CASCADE`: deleting the parent batch row must never destroy a report a
    # client has already been sent (task-47 §4 rationale).
    batch_id: Mapped[str | None] = mapped_column(
        GUID, ForeignKey("report_batches.id", ondelete="SET NULL"), default=None, index=True
    )
    kind: Mapped[str] = mapped_column(String(32))  # monthly|yearly|custom_months|comparator
    source: Mapped[str] = mapped_column(String(16))  # api|pdf|google_api (task-84)
    status: Mapped[str] = mapped_column(String(16), default="queued")
    # Report parameters only (years/span/months/force_refresh) — NEVER secrets
    # (PLAN §13): API keys/passwords/session cookies must never land here.
    params_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    period_count: Mapped[int] = mapped_column(Integer, default=0)
    periods_done: Mapped[int] = mapped_column(Integer, default=0)
    periods_failed: Mapped[int] = mapped_column(Integer, default=0)
    # Per-period progress AS DATA (task-45 §2.1), one dict per period:
    # `{label, date_from, date_to, is_partial, state, started_at,
    #   finished_at, duration_seconds, from_cache, error}`. Written in full
    # (every period `waiting`, dates already known) when the run is queued and
    # again when the worker starts it, then patched in place as each period
    # moves — so the UI reads per-period state instead of re-deriving it from
    # log message text, which is how task-43 had to do it (task-45 §1.1).
    periods_json: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, default=None)
    # NULL until a `source=pdf` run's cross-check actually runs (task-42 §3.6);
    # always NULL for `source=api` — never a stand-in "unknown" string.
    cross_check_status: Mapped[str | None] = mapped_column(String(16), default=None)
    # The finished 8xN table + cross-check cell details (task-42 §3.6/§5).
    result_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    error_code: Mapped[str | None] = mapped_column(String(100), default=None)
    error_message: Mapped[str | None] = mapped_column(String(1000), default=None)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    lease_owner: Mapped[str | None] = mapped_column(String(200), default=None)
    heartbeat_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)
    queued_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    # Хто створив цей КОНКРЕТНИЙ run (task-93) — при rebuild/retry це той,
    # хто натиснув кнопку, не автор джерела; при батчі — той, хто запустив
    # батч. Той самий словник і те саме `SET NULL`-міркування, що в
    # `ReportBatch` вище.
    created_by_user_id: Mapped[str | None] = mapped_column(
        GUID, ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    created_by_kind: Mapped[str | None] = mapped_column(String(16), default=None)


class ReportRunEvent(Base):
    """Progress/log line for one run (task-42 §2.5). `seq` is monotonic WITHIN
    a run (not global like `JobEvent.seq`) — a run's own SSE endpoint replays
    `Last-Event-ID` against `WHERE run_id=... AND seq>...`, so per-run
    numbering keeps the id meaningful without a shared counter across runs.
    """

    __tablename__ = "report_run_events"
    __table_args__ = (UniqueConstraint("run_id", "seq", name="uq_report_run_events_run_id_seq"),)

    id: Mapped[str] = mapped_column(GUID, primary_key=True, default=new_uuid)
    run_id: Mapped[str] = mapped_column(
        GUID, ForeignKey("report_runs.id", ondelete="CASCADE"), index=True
    )
    seq: Mapped[int] = mapped_column(Integer)
    level: Mapped[str] = mapped_column(String(16), default="info")  # debug|info|warning|error
    stage: Mapped[str | None] = mapped_column(String(64), default=None)
    message: Mapped[str] = mapped_column(String(2000), default="")
    details_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class ReportFile(Base):
    """One PDF/XLSX/screenshot artifact (task-42 §3.5/§3.7.4/§3.7.7).

    `run_id` is `SET NULL`, not `CASCADE`: deleting a run must never destroy a
    PDF that other runs have since reused via the closed-period cache
    (task-42 §3.5) — the index below is exactly that cache's lookup key.
    `period_label`/`date_from`/`date_to` are NULL for a run-level file (the
    XLSX covers every column, not one period).
    """

    __tablename__ = "report_files"
    __table_args__ = (
        Index(
            "ix_report_files_cache_lookup",
            "client_id",
            "source_report_name",
            "date_from",
            "date_to",
        ),
    )

    id: Mapped[str] = mapped_column(GUID, primary_key=True, default=new_uuid)
    client_id: Mapped[str] = mapped_column(
        GUID, ForeignKey("clients.id", ondelete="CASCADE"), index=True
    )
    run_id: Mapped[str | None] = mapped_column(
        GUID, ForeignKey("report_runs.id", ondelete="SET NULL"), default=None
    )
    kind: Mapped[str] = mapped_column(String(16))  # pdf|xlsx|screenshot
    period_label: Mapped[str | None] = mapped_column(String(64), default=None)
    date_from: Mapped[date | None] = mapped_column(Date, default=None)
    date_to: Mapped[date | None] = mapped_column(Date, default=None)
    is_partial_period: Mapped[bool] = mapped_column(Boolean, default=False)
    # Ninja report name the PDF was pulled from — only set for kind="pdf".
    source_report_name: Mapped[str | None] = mapped_column(String(200), default=None)
    # Relative to `Settings.artifacts_dir`, never absolute (mirrors `Artifact.path`).
    path: Mapped[str] = mapped_column(String(500))
    sha256: Mapped[str] = mapped_column(String(64))
    size_bytes: Mapped[int] = mapped_column(Integer)
    mime: Mapped[str | None] = mapped_column(String(100), default=None)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class CloudObject(Base):
    """Outbox ledger for the B2 cloud-storage sweeper (task-52 §3.3).

    Not a cache of B2's own listing — the source of truth for "has this
    already been uploaded". `UNIQUE (kind, source_ref)` is the idempotency
    guarantee a re-run of the sweeper relies on: a second attempt at the same
    `(kind, source_ref)` updates this row instead of creating a duplicate
    object in the bucket.
    """

    __tablename__ = "cloud_objects"
    __table_args__ = (
        UniqueConstraint("kind", "source_ref", name="uq_cloud_objects_kind_source_ref"),
        Index("ix_cloud_objects_kind_status", "kind", "status"),
    )

    id: Mapped[str] = mapped_column(GUID, primary_key=True, default=new_uuid)
    # "db_backup" | "report_file" | "monitor_blob" | "watermark".
    kind: Mapped[str] = mapped_column(String(32))
    # sha256 for monitor_blob, ReportFile.id for report_file, "<tier>/<date>"
    # for db_backup (e.g. "daily/2026-08-19"), a fixed name for watermark.
    source_ref: Mapped[str] = mapped_column(String(200))
    # B2 object key this ref maps to. For kind="watermark" this column holds
    # the watermark VALUE (an ISO datetime string), not a real bucket key —
    # a deliberate reuse of the same table for a single cutover marker
    # instead of a second table (task-52 §3.4).
    key: Mapped[str] = mapped_column(String(500))
    sha256: Mapped[str | None] = mapped_column(String(64), default=None)
    size_bytes: Mapped[int | None] = mapped_column(Integer, default=None)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    # Never a secret (PLAN §13) — B2 error strings never contain credentials.
    error_message: Mapped[str | None] = mapped_column(String(1000), default=None)
    uploaded_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


# --- Added by task-54 (Monthly Performance table, PLAN §16 "Хвиля Q") ------
#
# A cumulative register of months, NOT a `ReportRun` (task-42): GSC only
# keeps a 16-month window, so a row built once from a `ReportRun` would lose
# a month the moment it ages out of that window. `ClientMonthlyMetric` rows
# are mutable while `is_frozen=False` (re-collected on `/refresh`) and then
# left alone forever — the opposite of `MetricValue`'s immutable-on-commit
# rule, so these two models are deliberately NOT wired into `_reject_update`
# below.


class ClientMonthlyTableSettings(Base):
    """Per-client configuration, lazily created — same shape as
    `MonitorSettings.get_or_create_settings` (task-35): a client with no row
    yet reads as "unconfigured" (`key_event_names_json == []`) rather than
    needing a migration-time backfill for every existing client.
    """

    __tablename__ = "client_monthly_table_settings"
    __table_args__ = (
        UniqueConstraint("client_id", name="uq_client_monthly_table_settings_client_id"),
    )

    id: Mapped[str] = mapped_column(GUID, primary_key=True, default=new_uuid)
    client_id: Mapped[str] = mapped_column(GUID, ForeignKey("clients.id", ondelete="CASCADE"))
    # GA4 `eventName` values to sum into the named-event column (task-54
    # §1.5) — a plain list because several clients split one conversion
    # across per-location events ("Book Now 3rd Avenue", "... Hudson", ...).
    # `[]` means the column has never been configured for this client.
    key_event_names_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    # Column header for the named-event column ("Book Now", "Order Now") —
    # `None` while `key_event_names_json` is empty, hides the column.
    key_event_label: Mapped[str | None] = mapped_column(String(100), default=None)
    # "first_user" | "session" — which GA4 channel-group field decides
    # "Organic Search" for each half of the table (task-54 §1.3/§2.2): the
    # default mix (users=first_user, events=session) is what live-matched
    # the operator's own sheet exactly, not a technical default.
    attribution_users: Mapped[str] = mapped_column(String(16), default="first_user")
    attribution_events: Mapped[str] = mapped_column(String(16), default="session")
    # `None` = use the global `client_monthly_history_months` setting.
    history_months: Mapped[int | None] = mapped_column(Integer, default=None)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


class ClientMonthlyMetric(Base):
    """One row per `(client, month)` — the Monthly Performance table's own
    durable store (task-54 §2.2), independent of any `ReportRun`.

    `is_frozen=True` once a month is closed, settled (>=3 days past close,
    GA4 keeps revising for ~48h) and its last collection attempt succeeded
    (`ga4_state`/`gsc_state` in `ok`/`no_data`/`not_configured` — never
    `failed`, task-54 §2.2): a frozen row is never re-queried, which is what
    keeps a month from vanishing once it ages out of GSC's 16-month window.
    `attribution_users`/`attribution_events`/`key_event_names_json` are
    duplicated from the settings that produced THIS row (not read live from
    `ClientMonthlyTableSettings`) so that changing the client's attribution
    later can never silently reinterpret an already-frozen row — the exact
    failure mode that made the operator's own sheet mix models undetected
    (task-54 §1.2).
    """

    __tablename__ = "client_monthly_metrics"
    __table_args__ = (
        UniqueConstraint("client_id", "month", name="uq_client_monthly_metrics_client_id_month"),
        Index("ix_client_monthly_metrics_client_id", "client_id"),
    )

    id: Mapped[str] = mapped_column(GUID, primary_key=True, default=new_uuid)
    client_id: Mapped[str] = mapped_column(GUID, ForeignKey("clients.id", ondelete="CASCADE"))
    month: Mapped[str] = mapped_column(String(7))  # "YYYY-MM"
    organic_users: Mapped[int | None] = mapped_column(Integer, default=None)
    organic_conversions: Mapped[int | None] = mapped_column(Integer, default=None)
    key_event_count: Mapped[int | None] = mapped_column(Integer, default=None)
    gsc_clicks: Mapped[int | None] = mapped_column(Integer, default=None)
    gsc_impressions: Mapped[int | None] = mapped_column(Integer, default=None)
    # How many day-rows GSC actually returned for this month (task-54 §1.4б)
    # — the `date`-dimensioned discriminator between "no data" and "a real
    # zero", and the source of the `partial` badge (e.g. 23 of 30 days at
    # the edge of the 16-month window).
    gsc_days_with_data: Mapped[int | None] = mapped_column(Integer, default=None)
    days_in_month: Mapped[int] = mapped_column(Integer)
    attribution_users: Mapped[str] = mapped_column(String(16))
    attribution_events: Mapped[str] = mapped_column(String(16))
    key_event_names_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    is_frozen: Mapped[bool] = mapped_column(Boolean, default=False)
    ga4_state: Mapped[str] = mapped_column(String(20), default="ok")
    gsc_state: Mapped[str] = mapped_column(String(20), default="ok")
    # Set when a query for this row was answered with `limit` rows exactly
    # (task-54 §2.3) — a possible silent truncation, surfaced rather than
    # rendered as a trustworthy total.
    truncated: Mapped[bool] = mapped_column(Boolean, default=False)
    collected_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


class SchemaMarkupCheck(Base):
    """Один прогін інструмента Schema Markup Checker (task-56).

    Та сама форма, що `ContentQaReport`/`RedirectCheck`: один рядок = один
    прогін, без per-property мутабельного стану всередині — весь звіт
    незмінний (без `updated_at`), повторна перевірка того самого URL створює
    новий рядок, історія важлива (той самий URL міг раніше не мати розмітки,
    а тепер мати — це видима зміна, не те, що варто затерти).

    `client_id` опційний: перевіряють і чужі/проспектні сторінки, не лише
    клієнтів дашборду.
    """

    __tablename__ = "schema_markup_checks"
    __table_args__ = (Index("ix_schema_markup_checks_created_at", "created_at"),)

    id: Mapped[str] = mapped_column(GUID, primary_key=True, default=new_uuid)
    client_id: Mapped[str | None] = mapped_column(
        GUID, ForeignKey("clients.id", ondelete="SET NULL"), default=None
    )
    url: Mapped[str] = mapped_column(String(2048))
    final_url: Mapped[str | None] = mapped_column(String(2048), default=None)
    http_status: Mapped[int | None] = mapped_column(Integer, default=None)
    # "clean" | "has_gaps" | "missing" | "fetch_failed" — `app.tools.schema_markup.service.Verdict`.
    verdict: Mapped[str] = mapped_column(String(16))
    page_kind: Mapped[str | None] = mapped_column(String(32), default=None)
    found_types_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    findings_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    generated_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    # Кожен елемент — `{"format": "json-ld"|"microdata", "raw": ..., "error": str|None}`.
    # `raw` для JSON-LD — вузол/граф як його реально написав плагін (Yoast/RankMath/…),
    # для microdata — те саме дерево, зібране з `itemprop`/`itemscope` у формі JSON
    # (`ExtractedEntity.to_jsonable()`), бо своєї нативної JSON-форми в microdata немає.
    # Показується оператору в UI «Raw markup» — не лише список типів, а те, що реально
    # лежить на сторінці, для звірки вручну.
    raw_markup_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    vocab_version: Mapped[str] = mapped_column(String(32))
    error: Mapped[str | None] = mapped_column(String(500), default=None)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class PageInventorySnapshot(Base):
    """One built Page Inventory table for one client (task-57 §3).

    A client keeps exactly one active snapshot — a rebuild deletes the
    previous row (`ondelete="CASCADE"` on `PageInventoryItem.snapshot_id`
    takes its items with it) and inserts a new one. Full QA history across
    rebuilds isn't kept (task-57 §11 follow-up); this table exists so the
    UI and the XLSX export always read the exact same already-computed
    findings, rather than two independent re-runs of the QA engine.
    """

    __tablename__ = "page_inventory_snapshots"

    id: Mapped[str] = mapped_column(GUID, primary_key=True, default=new_uuid)
    client_id: Mapped[str] = mapped_column(
        GUID, ForeignKey("clients.id", ondelete="CASCADE"), unique=True, index=True
    )
    built_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    # Latest `MonitorPage.last_checked_at` seen among the rows this snapshot
    # was built from — "as-of" freshness shown in the UI (task-57 §2.2),
    # since the inventory reads monitor's LAST crawl, not a live fetch.
    source_collected_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)
    pages_total: Mapped[int] = mapped_column(Integer, default=0)
    pages_unique: Mapped[int] = mapped_column(Integer, default=0)
    pages_with_breadcrumbs: Mapped[int] = mapped_column(Integer, default=0)
    pages_with_keywords: Mapped[int] = mapped_column(Integer, default=0)
    # "json_ld" | "dom" | "mixed" | "none" (task-57 §1.2).
    breadcrumb_source: Mapped[str] = mapped_column(String(16), default="none")
    warnings_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class PageInventoryItem(Base):
    """One row of a `PageInventorySnapshot` — one canonical URL, its
    breadcrumb trail, meta fields and the full (uncalibrated) QA finding
    set for it. Profile calibration (task-57 §2.3) is applied on READ, not
    stored — `findings_json` always holds what `strict` would show."""

    __tablename__ = "page_inventory_items"
    __table_args__ = (
        Index("ix_page_inventory_items_snapshot_status", "snapshot_id", "status_label"),
    )

    id: Mapped[str] = mapped_column(GUID, primary_key=True, default=new_uuid)
    snapshot_id: Mapped[str] = mapped_column(
        GUID, ForeignKey("page_inventory_snapshots.id", ondelete="CASCADE"), index=True
    )
    client_id: Mapped[str] = mapped_column(GUID, ForeignKey("clients.id", ondelete="CASCADE"))
    url: Mapped[str] = mapped_column(String(2048))
    canonical_key: Mapped[str] = mapped_column(String(2048))
    duplicate_urls_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    page_label: Mapped[str] = mapped_column(String(500), default="")
    breadcrumbs_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    breadcrumb_source: Mapped[str] = mapped_column(String(16), default="none")
    # `None` = page never checked by the monitor (PLAN §12); `""` = checked,
    # tag absent from the fetched HTML — a real defect, not a missing source
    # (task-57 §1.6, the deliberate inversion of `app.contentqa`'s own rule).
    title: Mapped[str | None] = mapped_column(String(500), default=None)
    # task-75 §3.1: was `String(1000)`. Live production data has meta
    # descriptions up to 8385 chars (SQLite never enforced the declared
    # VARCHAR length, so this went unnoticed until a Postgres migration
    # dry-run rejected them with `StringDataRightTruncation`). `Text` has
    # no natural cap and costs nothing extra on Postgres over `varchar`.
    description: Mapped[str | None] = mapped_column(Text, default=None)
    keywords_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    keywords_source: Mapped[str] = mapped_column(String(8), default="none")
    h1: Mapped[str | None] = mapped_column(String(500), default=None)
    status: Mapped[int | None] = mapped_column(Integer, default=None)
    status_label: Mapped[str] = mapped_column(String(16), default="not_checked")
    indexable: Mapped[bool | None] = mapped_column(Boolean, default=None)
    canonical_missing: Mapped[bool] = mapped_column(Boolean, default=False)
    checked: Mapped[bool] = mapped_column(Boolean, default=False)
    # [{check, severity, field, detail, value}, ...] — the FULL (strict)
    # finding set; `app.pageinventory.calibrate` derives `calibrated` from
    # this on read, never the other way around.
    findings_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)


# --- Added by task-59 (Web Archive, PLAN §16 wave-U extension) ------------
#
# A self-hosted, Wayback-style copy of every client's site: twice-daily
# capture, content-addressed dedup, replay. Deliberately its OWN tables,
# independent of `monitor_*` above (task-35) — the monitor is a cheap,
# hourly watchdog with no subresources; this is a full archive. See
# `app/webarchive/__init__.py` for why the two are not merged.


class ArchiveSettings(Base):
    """Per-client Web Archive configuration. Lazily created on first touch —
    `WebArchiveRepository.get_or_create_settings` — same pattern as
    `MonitorSettings`, so a client with no row yet still reads as "archive
    enabled with the global defaults" rather than needing a migration-time
    backfill for every existing client.
    """

    __tablename__ = "archive_settings"
    __table_args__ = (UniqueConstraint("client_id", name="uq_archive_settings_client_id"),)

    id: Mapped[str] = mapped_column(GUID, primary_key=True, default=new_uuid)
    client_id: Mapped[str] = mapped_column(GUID, ForeignKey("clients.id", ondelete="CASCADE"))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    capture_assets: Mapped[bool] = mapped_column(Boolean, default=True)
    # `None` = use the global `archive_max_urls_per_client` setting.
    max_urls: Mapped[int | None] = mapped_column(Integer, default=None)
    ignore_patterns_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    include_query_strings: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


class ArchiveCapture(Base):
    """One capture pass (a scheduled 13:30/19:30 Kyiv slot, or a manual
    request) for one client (task-59 §1.4).

    `slot_key` carries the UTC-offset-qualified Kyiv slot identity (e.g.
    `2026-09-04T13:30+03:00`) rather than relying on scheduler-thread memory
    for idempotency — a worker restart mid-slot re-derives "has this slot
    already run" from THIS unique constraint, not from an in-memory set, so
    it neither duplicates nor silently skips a slot across a restart.
    """

    __tablename__ = "archive_captures"
    __table_args__ = (
        UniqueConstraint("client_id", "slot_key", name="uq_archive_captures_client_id_slot_key"),
        Index("ix_archive_captures_client_started", "client_id", "started_at"),
        Index("ix_archive_captures_status_queued_at", "status", "queued_at"),
    )

    id: Mapped[str] = mapped_column(GUID, primary_key=True, default=new_uuid)
    client_id: Mapped[str] = mapped_column(
        GUID, ForeignKey("clients.id", ondelete="CASCADE"), index=True
    )
    slot_key: Mapped[str] = mapped_column(String(64))
    scheduled_for: Mapped[datetime] = mapped_column(UTCDateTime)
    trigger: Mapped[str] = mapped_column(String(16), default="scheduled")  # "scheduled"|"manual"
    # "queued" | "running" | "ok" | "partial" | "failed" | "blocked"
    status: Mapped[str] = mapped_column(String(16), default="queued", index=True)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)
    # `None` for a run that never got far enough to count its own target
    # list (PLAN §12: absence of data is never coerced into 0).
    urls_total: Mapped[int | None] = mapped_column(Integer, default=None)
    urls_captured: Mapped[int] = mapped_column(Integer, default=0)
    urls_unchanged: Mapped[int] = mapped_column(Integer, default=0)
    urls_new: Mapped[int] = mapped_column(Integer, default=0)
    urls_failed: Mapped[int] = mapped_column(Integer, default=0)
    urls_blocked: Mapped[int] = mapped_column(Integer, default=0)
    assets_fetched: Mapped[int] = mapped_column(Integer, default=0)
    assets_reused: Mapped[int] = mapped_column(Integer, default=0)
    assets_failed: Mapped[int] = mapped_column(Integer, default=0)
    bytes_stored: Mapped[int] = mapped_column(Integer, default=0)
    error_code: Mapped[str | None] = mapped_column(String(100), default=None)
    error_message: Mapped[str | None] = mapped_column(String(1000), default=None)
    # task-62: a real FIFO queue + a liveness signal that survives process
    # boundaries. `queued_at` orders `claim_capture`'s pick (`scheduled_for`
    # used to double as this for manual runs, but that reads oddly once the
    # queue has more than one waiting row). `owner`/`heartbeat_at` replace
    # `ArchiveScheduler._in_flight` (in-process memory only) as the "is this
    # capture actually alive" check — a CLI-run capture, in its own OS
    # process, was invisible to that in-memory set and got declared
    # `interrupted` within a minute despite running to completion (live,
    # 2026-09-05). `scope`/`archive_url_id` let a capture target exactly one
    # URL instead of a whole client (task-62 §2.7).
    queued_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)
    owner: Mapped[str | None] = mapped_column(String(64), default=None)
    heartbeat_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    scope: Mapped[str] = mapped_column(String(16), default="client")  # "client" | "url"
    archive_url_id: Mapped[str | None] = mapped_column(
        GUID, ForeignKey("archive_urls.id", ondelete="SET NULL"), default=None
    )
    progress_message: Mapped[str | None] = mapped_column(String(500), default=None)
    # task-80: "Recapture" — примусова нова копія навіть незміненої сторінки.
    # Живе на рядку, не в запиті: обхід робить воркер уже ПІСЛЯ того, як
    # HTTP-запит завершився (та сама причина, що й `scope`/`archive_url_id`
    # вище) — планувальник мусить прочитати це поле назад із `claimed`,
    # інакше кнопка тихо виконує звичайний капчур (той самий клас дефекту,
    # що вже стався живо з `only_url_id`, task-62 §2.7).
    force: Mapped[bool] = mapped_column(Boolean, default=False)


class ArchiveUrl(Base):
    """One URL of one client tracked by the archive (task-59 §1.9) — the
    monitor's own `monitor_pages` seeds this, plus same-host links
    discovered while rewriting a captured page (`source="link"`).
    """

    __tablename__ = "archive_urls"
    __table_args__ = (
        UniqueConstraint("client_id", "url_key", name="uq_archive_urls_client_id_url_key"),
    )

    id: Mapped[str] = mapped_column(GUID, primary_key=True, default=new_uuid)
    client_id: Mapped[str] = mapped_column(
        GUID, ForeignKey("clients.id", ondelete="CASCADE"), index=True
    )
    url: Mapped[str] = mapped_column(String(2048))
    url_key: Mapped[str] = mapped_column(String(2048))
    source: Mapped[str] = mapped_column(String(16), default="sitemap")  # sitemap|link|manual
    first_captured_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)
    last_captured_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)
    snapshot_count: Mapped[int] = mapped_column(Integer, default=0)
    # Set the capture a URL first fails to resolve/gone; cleared if it comes
    # back (mirrors `MonitorPage.gone_since`).
    gone_since: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class ArchiveBlob(Base):
    """Content-addressed byte registry + upload state (task-59 §1.8) — own
    `webarchive/` B2 prefix and own ledger, deliberately NOT a reuse of
    task-52's `cloud_objects`/`CloudLedgerRepository` (this module's sweeper
    is self-contained, see `app/webarchive/cloud_sync.py`).
    """

    __tablename__ = "archive_blobs"

    sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    size_bytes: Mapped[int] = mapped_column(Integer)
    gzip_size_bytes: Mapped[int] = mapped_column(Integer)
    content_type: Mapped[str | None] = mapped_column(String(200), default=None)
    # Relative to the artifacts root, never absolute.
    path: Mapped[str] = mapped_column(String(500))
    first_seen_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    uploaded_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)
    local_deleted_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)


class ArchiveSnapshot(Base):
    """One captured state of one `ArchiveUrl` (task-59 §1.2) — written ONLY
    when `content_fingerprint` differs from the previous snapshot, the
    previous one is older than `archive_refresh_after_days`, or the HTTP
    status/final URL changed. An unchanged capture instead just calls
    `WebArchiveRepository.confirm_snapshot` on the existing row.

    Immutable after commit except `last_confirmed_at`/`confirmed_count` —
    see `_reject_mutation_except` below, which allow-lists exactly those two
    so "was this version still live as of the last check" can be recorded
    without opening up the historical record itself to edits.
    """

    __tablename__ = "archive_snapshots"
    __table_args__ = (
        Index("ix_archive_snapshots_url_observed", "archive_url_id", "observed_at"),
        Index("ix_archive_snapshots_client_observed", "client_id", "observed_at"),
    )

    id: Mapped[str] = mapped_column(GUID, primary_key=True, default=new_uuid)
    archive_url_id: Mapped[str] = mapped_column(
        GUID, ForeignKey("archive_urls.id", ondelete="CASCADE"), index=True
    )
    client_id: Mapped[str] = mapped_column(GUID, ForeignKey("clients.id", ondelete="CASCADE"))
    capture_id: Mapped[str | None] = mapped_column(
        GUID, ForeignKey("archive_captures.id", ondelete="SET NULL"), default=None
    )
    observed_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    http_status: Mapped[int | None] = mapped_column(Integer, default=None)
    final_url: Mapped[str | None] = mapped_column(String(2048), default=None)
    # `Set-Cookie` is stripped before this is written (PLAN §13: no secrets
    # in stored data) — see `app.webarchive.capture`.
    headers_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    content_type: Mapped[str | None] = mapped_column(String(200), default=None)
    content_fingerprint: Mapped[str | None] = mapped_column(String(64), default=None)
    raw_sha256: Mapped[str | None] = mapped_column(String(64), default=None)
    body_blob_sha256: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("archive_blobs.sha256", ondelete="SET NULL"), default=None
    )
    title: Mapped[str | None] = mapped_column(String(500), default=None)
    # task-75 §3.1: was `String(1000)`. Live production data has meta
    # descriptions up to 8385 chars (SQLite never enforced the declared
    # VARCHAR length, so this went unnoticed until a Postgres migration
    # dry-run rejected them with `StringDataRightTruncation`). `Text` has
    # no natural cap and costs nothing extra on Postgres over `varchar`.
    description: Mapped[str | None] = mapped_column(Text, default=None)
    canonical: Mapped[str | None] = mapped_column(String(2048), default=None)
    meta_robots: Mapped[str | None] = mapped_column(String(200), default=None)
    word_count: Mapped[int | None] = mapped_column(Integer, default=None)
    asset_count: Mapped[int] = mapped_column(Integer, default=0)
    assets_truncated: Mapped[bool] = mapped_column(Boolean, default=False)
    # "first" | "changed" | "weekly_refresh" | "status_changed"
    store_reason: Mapped[str] = mapped_column(String(16), default="first")
    prev_snapshot_id: Mapped[str | None] = mapped_column(
        GUID, ForeignKey("archive_snapshots.id", ondelete="SET NULL"), default=None
    )
    last_confirmed_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    confirmed_count: Mapped[int] = mapped_column(Integer, default=1)


class ArchiveAsset(Base):
    """One subresource URL, globally deduplicated across every client
    (task-59 §1.3) — the same Google Font or CDN library referenced by five
    clients is tracked, and re-validated, exactly once.
    """

    __tablename__ = "archive_assets"
    __table_args__ = (UniqueConstraint("url_key", name="uq_archive_assets_url_key"),)

    id: Mapped[str] = mapped_column(GUID, primary_key=True, default=new_uuid)
    url: Mapped[str] = mapped_column(String(2048))
    url_key: Mapped[str] = mapped_column(String(2048))
    kind: Mapped[str] = mapped_column(String(16), default="other")  # css|js|img|font|media|other
    first_seen_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    last_checked_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)
    etag: Mapped[str | None] = mapped_column(String(200), default=None)
    last_modified: Mapped[str | None] = mapped_column(String(100), default=None)
    failure_count: Mapped[int] = mapped_column(Integer, default=0)


class ArchiveAssetVersion(Base):
    """One captured content version of one `ArchiveAsset`, valid over a time
    window (task-59 §1.3). Immutable after commit except
    `last_confirmed_at` — a `304` on re-validation only extends the window.
    """

    __tablename__ = "archive_asset_versions"
    __table_args__ = (
        Index("ix_archive_asset_versions_asset_confirmed", "asset_id", "last_confirmed_at"),
    )

    id: Mapped[str] = mapped_column(GUID, primary_key=True, default=new_uuid)
    asset_id: Mapped[str] = mapped_column(
        GUID, ForeignKey("archive_assets.id", ondelete="CASCADE"), index=True
    )
    blob_sha256: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("archive_blobs.sha256", ondelete="SET NULL"), default=None
    )
    content_type: Mapped[str | None] = mapped_column(String(200), default=None)
    http_status: Mapped[int | None] = mapped_column(Integer, default=None)
    first_seen_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    last_confirmed_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class ArchiveSnapshotAsset(Base):
    """Which asset version was actually on the page for one snapshot
    (task-59 §1.3) — drives the Meta panel's asset list and
    `assets_truncated` accounting. `asset_version_id` is `None` when the
    asset was referenced but could not be captured (PLAN §12: a real
    failure, never silently dropped from the list).
    """

    __tablename__ = "archive_snapshot_assets"
    __table_args__ = (Index("ix_archive_snapshot_assets_snapshot", "snapshot_id"),)

    id: Mapped[str] = mapped_column(GUID, primary_key=True, default=new_uuid)
    snapshot_id: Mapped[str] = mapped_column(
        GUID, ForeignKey("archive_snapshots.id", ondelete="CASCADE"), index=True
    )
    asset_id: Mapped[str] = mapped_column(GUID, ForeignKey("archive_assets.id", ondelete="CASCADE"))
    asset_version_id: Mapped[str | None] = mapped_column(
        GUID, ForeignKey("archive_asset_versions.id", ondelete="SET NULL"), default=None
    )
    role: Mapped[str] = mapped_column(String(16), default="other")  # css|js|img|font|media|other


class ArchiveChange(Base):
    """The "what changed when" feed (task-59 §5) — a diff summary computed
    at capture time between two consecutive snapshots of the same URL.
    Deliberately does NOT reuse `MonitorChange` — different scope (whole
    rendered page vs SEO signals) and a different consumer (`#/archive/changes`
    vs the monitor's alert feed), same reasoning `MonitorChange`'s own
    docstring gives for not reusing "alerts" across unrelated concepts.
    """

    __tablename__ = "archive_changes"
    __table_args__ = (Index("ix_archive_changes_client_detected", "client_id", "detected_at"),)

    id: Mapped[str] = mapped_column(GUID, primary_key=True, default=new_uuid)
    client_id: Mapped[str] = mapped_column(
        GUID, ForeignKey("clients.id", ondelete="CASCADE"), index=True
    )
    archive_url_id: Mapped[str] = mapped_column(
        GUID, ForeignKey("archive_urls.id", ondelete="CASCADE"), index=True
    )
    snapshot_id: Mapped[str] = mapped_column(
        GUID, ForeignKey("archive_snapshots.id", ondelete="CASCADE")
    )
    prev_snapshot_id: Mapped[str | None] = mapped_column(
        GUID, ForeignKey("archive_snapshots.id", ondelete="SET NULL"), default=None
    )
    detected_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    # title|description|h1|text|links|images|status|redirect|noindex
    change_kinds_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    summary_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    text_delta_words: Mapped[int] = mapped_column(Integer, default=0)


class ArchiveReplayMiss(Base):
    """Every replay request that resolved to neither an `ArchiveUrl` nor an
    `ArchiveAsset` (task-59 §1.6/§4) — the archive's own honest list of
    rewriter gaps, discovered from real traffic instead of guessed at.
    """

    __tablename__ = "archive_replay_misses"
    __table_args__ = (UniqueConstraint("url_key", name="uq_archive_replay_misses_url_key"),)

    id: Mapped[str] = mapped_column(GUID, primary_key=True, default=new_uuid)
    url: Mapped[str] = mapped_column(String(2048))
    url_key: Mapped[str] = mapped_column(String(2048))
    hit_count: Mapped[int] = mapped_column(Integer, default=1)
    first_seen_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class ArchiveCaptureEvent(Base):
    """Progress/log line for one capture (task-62 §2.4) — same shape as
    `ReportRunEvent`: `seq` is monotonic WITHIN a capture, not global, so a
    capture's own log endpoint replays `since=` against
    `WHERE capture_id=... AND seq>...` without a shared counter across runs.
    `run_client_capture` writes nothing anywhere else — this table is the
    only per-URL trace of what a capture actually did (task-62 §0.5).
    """

    __tablename__ = "archive_capture_events"
    __table_args__ = (
        UniqueConstraint("capture_id", "seq", name="uq_archive_capture_events_capture_id_seq"),
    )

    id: Mapped[str] = mapped_column(GUID, primary_key=True, default=new_uuid)
    capture_id: Mapped[str] = mapped_column(
        GUID, ForeignKey("archive_captures.id", ondelete="CASCADE"), index=True
    )
    seq: Mapped[int] = mapped_column(Integer)
    level: Mapped[str] = mapped_column(String(16), default="info")  # debug|info|warning|error
    stage: Mapped[str | None] = mapped_column(String(64), default=None)
    message: Mapped[str] = mapped_column(String(2000), default="")
    details_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


# --- Added by task-64 (URL Inspector screenshots, task-64 §2) ---------------
#
# Inherited-zone addition: takes over migration/model ownership for exactly
# this one table, same precedent as task-16's `GscDimensionMetric` above.
# Deliberately its OWN table under `settings.artifacts_dir / "urlshots"`, not
# a row referencing the shared `monitor_blobs`/`archive_*` blob stores —
# `app.cloud.sweeper` prunes blobs that carry no row in ITS OWN tables, and a
# screenshot living in a shared blob root without one of those rows is
# exactly the "unreferenced blob" shape that sweeper is built to delete.


class UrlScreenshot(Base):
    """One rendered screenshot of one URL at one viewport preset (task-64 §2).

    Multiple rows per `(inspector_key, preset)` are kept over time — never
    overwritten — so "how did this page look a month ago" falls out of the
    same table as "how does it look now", the same reasoning `ArchiveSnapshot`
    already uses for HTML. `state="failed"` rows carry no blob (PLAN §12: a
    failed capture is a real outcome, not a missing row) — `error` says why.
    """

    __tablename__ = "url_screenshots"
    __table_args__ = (
        Index(
            "ix_url_screenshots_key_preset_captured",
            "inspector_key",
            "preset",
            "captured_at",
        ),
    )

    id: Mapped[str] = mapped_column(GUID, primary_key=True, default=new_uuid)
    # Nullable: a screenshot can be taken for a URL whose host matches no
    # known client (same reasoning as `SchemaMarkupCheck.client_id`).
    client_id: Mapped[str | None] = mapped_column(
        GUID, ForeignKey("clients.id", ondelete="SET NULL"), default=None
    )
    inspector_key: Mapped[str] = mapped_column(String(2048))
    url: Mapped[str] = mapped_column(String(2048))
    preset: Mapped[str] = mapped_column(String(16))  # "desktop" | "tablet" | "mobile"
    state: Mapped[str] = mapped_column(String(8), default="ok")  # "ok" | "failed"
    blob_sha256: Mapped[str | None] = mapped_column(String(64), default=None)
    thumb_sha256: Mapped[str | None] = mapped_column(String(64), default=None)
    width: Mapped[int | None] = mapped_column(Integer, default=None)
    height: Mapped[int | None] = mapped_column(Integer, default=None)
    # Viewport height ("the fold") — distinct from `height`, the full
    # captured page height, so the UI can draw a fold line on the image.
    fold_height: Mapped[int | None] = mapped_column(Integer, default=None)
    byte_size: Mapped[int | None] = mapped_column(Integer, default=None)
    # A full-page capture past `_MAX_FULL_HEIGHT_PX` is clipped, not silently
    # truncated (PLAN §12) — this flag says so.
    truncated: Mapped[bool] = mapped_column(Boolean, default=False)
    http_status: Mapped[int | None] = mapped_column(Integer, default=None)
    final_url: Mapped[str | None] = mapped_column(String(2048), default=None)
    error: Mapped[str | None] = mapped_column(String(500), default=None)
    captured_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


def _reject_update(mapper: Any, connection: Any, target: Base) -> None:
    raise ValueError(f"{type(target).__name__} rows are immutable after commit")


class User(Base):
    """Співробітник агенції (task-71).

    Рядки НЕ видаляються: `is_active=False` + відкликання сесій. Видалення
    зробило б історію дій (task-72) безавторською, а `created_by_id` нижче —
    висячим посиланням.
    """

    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("email", name="uq_users_email"),)

    id: Mapped[str] = mapped_column(GUID, primary_key=True, default=new_uuid)
    # Завжди у нижньому регістрі (`schemas.auth.normalize_email`) — унікальність
    # у SQLite чутлива до регістру, тож канонізація робиться на вході, а не
    # `COLLATE NOCASE` (той ще й ламає використання індексу для LIKE).
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200))
    role: Mapped[UserRole] = mapped_column(_str_enum(UserRole, 16), default=UserRole.member)
    # `scrypt$<n>$<r>$<p>$<b64 salt>$<b64 hash>` — див. app/core/auth/passwords.py.
    # Пароль генерує сервер і користувач його не змінює (рішення оператора:
    # так працюють інші тули компанії), тому поля `must_change_password` тут
    # свідомо немає — перевидати може лише адмін.
    password_hash: Mapped[str] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    password_set_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    failed_attempts: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)
    last_login_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)
    # NULL у bootstrap-адміна: його створює entrypoint, а не людина.
    created_by_id: Mapped[str | None] = mapped_column(
        GUID, ForeignKey("users.id", ondelete="SET NULL"), default=None
    )


class UserSession(Base):
    """Одна активна сесія браузера (task-71).

    У БД лежить лише `sha256` від токена: витік дампа таблиці не дає нікому
    можливості представитись користувачем — той самий принцип, що PLAN §13
    вимагає для будь-яких секретів.
    """

    __tablename__ = "user_sessions"
    __table_args__ = (
        UniqueConstraint("token_hash", name="uq_user_sessions_token_hash"),
        Index("ix_user_sessions_user_id_expires_at", "user_id", "expires_at"),
    )

    id: Mapped[str] = mapped_column(GUID, primary_key=True, default=new_uuid)
    user_id: Mapped[str] = mapped_column(
        GUID, ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    # Другий, незалежний секрет — double-submit CSRF (task-71 §5). Не HttpOnly
    # на боці браузера, тому НІКОЛИ не використовується для автентифікації.
    csrf_token: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    # Оновлюється не частіше, ніж раз на `session_touch_interval_seconds`:
    # після task-66/67 (writer-лок був зайнятий 97% часу) запис на кожен
    # запит — це податок на всю систему, а не на одну сесію.
    last_seen_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)
    ip: Mapped[str | None] = mapped_column(String(64), default=None)
    user_agent: Mapped[str | None] = mapped_column(String(256), default=None)


class AgentToken(Base):
    """Особистий bearer-токен для API (task-79) — «кожен видає собі сам».

    На відміну від `UserSession` (браузерна cookie-сесія), цей рядок живе
    довго й навмисно не прив'язаний до жодного `Request`: створюється в
    кабінеті, використовується скриптами/MCP-хостами/CI. Той самий принцип
    зберігання, що й сесії — у БД лежить лише `token_sha256`, сире значення
    існує рівно в одній HTTP-відповіді (`POST /api/v1/me/tokens`) і ніде
    більше.

    `role` — стеля цього КОНКРЕТНОГО токена, не користувача: адмін може
    видати собі `viewer`-токен для read-only скриптів (навмисно дозволено,
    §3.3 плану). Фактичні права на кожен запит — `min(role, поточна роль
    власника)` (`app/core/auth/sessions.py::resolve_personal_token_sync`),
    тому пониження людини чи `is_active=False` вбиває токен негайно, без
    окремого механізму відкликання.

    Рядки НЕ видаляються (`revoked_at`) — та сама причина, що й у `User`:
    журнал дій (task-72) не має лишитись без автора.
    """

    __tablename__ = "agent_tokens"
    __table_args__ = (UniqueConstraint("token_sha256", name="uq_agent_tokens_token_sha256"),)

    id: Mapped[str] = mapped_column(GUID, primary_key=True, default=new_uuid)
    user_id: Mapped[str] = mapped_column(
        GUID, ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    label: Mapped[str] = mapped_column(String(200))
    token_sha256: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    # Перші 8 символів сирого значення (після префіксу `yvth_`) — НЕ секрет,
    # достатньо короткий, щоб не дати нічого перебору, але достатній, щоб
    # людина впізнала СВІЙ токен у списку без другого показу повного значення.
    prefix: Mapped[str] = mapped_column(String(8))
    role: Mapped[UserRole] = mapped_column(_str_enum(UserRole, 16))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)
    last_used_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)
    # Заповнене лише для токенів, виданих OAuth-потоком (task-78): який
    # саме конектор його отримав. Для токенів, створених людиною в
    # кабінеті, лишається `None` — вони не належать жодному клієнту.
    oauth_client_id: Mapped[str | None] = mapped_column(String(64), default=None)


class OAuthClient(Base):
    """Клієнт, який зареєструвався через DCR (RFC 7591) — task-78.

    Claude реєструє новий client_id на КОЖНЕ свіже підключення (це прямо
    описано в документації конекторів як властивість DCR), тож таблиця
    росте від підключень, а не від користувачів; вона навмисно дешева.

    Секрет тут не зберігається взагалі, і це рішення, а не пропуск: ми
    видаємо виключно public-клієнтів (`token_endpoint_auth_method="none"`)
    і покладаємось на PKCE S256, якого Claude вимагає на кожному запиті.
    Так інваріант проєкту «секрети не потрапляють у БД» лишається цілим
    без окремого хешування й порівняння.
    """

    __tablename__ = "oauth_clients"

    client_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    # Повний JSON реєстрації (RFC 7591) — SDK віддає й приймає цю модель
    # цілком, і зберігати її розібраною по колонках означало б ловити
    # кожне нове необов'язкове поле специфікації міграцією.
    client_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class OAuthAuthorizationCode(Base):
    """Одноразовий код авторизації між нашим екраном згоди й `/token`.

    Живе хвилини й гине після першого обміну (`used_at`) — повторне
    пред'явлення коду має провалитись, інакше перехоплений код можна
    обміняти вдруге.
    """

    __tablename__ = "oauth_auth_codes"
    __table_args__ = (
        UniqueConstraint("code_sha256", name="uq_oauth_auth_codes_code_sha256"),
    )

    id: Mapped[str] = mapped_column(GUID, primary_key=True, default=new_uuid)
    code_sha256: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    client_id: Mapped[str] = mapped_column(String(64), index=True)
    user_id: Mapped[str] = mapped_column(
        GUID, ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    scopes: Mapped[str] = mapped_column(Text, default="")
    code_challenge: Mapped[str] = mapped_column(String(200))
    redirect_uri: Mapped[str] = mapped_column(Text)
    redirect_uri_provided_explicitly: Mapped[bool] = mapped_column(Boolean, default=True)
    resource: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)
    used_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)


class OAuthPendingAuthorization(Base):
    """Запит на авторизацію, що чекає на людину (task-78).

    Між `/authorize` і натисканням Approve стоїть логін і екран згоди —
    тобто довільно довга пауза з переходами по сторінках. Параметри
    запиту (PKCE-виклик, redirect_uri, state) мусять пережити цю паузу,
    але не мусять їхати в URL: те, що лежить у рядку адреси, потрапляє в
    історію браузера й у логи проксі. Тому в URL іде лише непрозорий id
    цього рядка.
    """

    __tablename__ = "oauth_pending_authorizations"

    id: Mapped[str] = mapped_column(GUID, primary_key=True, default=new_uuid)
    client_id: Mapped[str] = mapped_column(String(64), index=True)
    scopes: Mapped[str] = mapped_column(Text, default="")
    code_challenge: Mapped[str] = mapped_column(String(200))
    redirect_uri: Mapped[str] = mapped_column(Text)
    redirect_uri_provided_explicitly: Mapped[bool] = mapped_column(Boolean, default=True)
    state: Mapped[str | None] = mapped_column(Text, default=None)
    resource: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)


class OAuthRefreshToken(Base):
    """Refresh-токен конектора (task-78).

    Окрема таблиця, а не рядок в `agent_tokens`, бо це різні сутності:
    `agent_tokens` — те, чим ходять в API, і що людина бачить і відкликає
    в кабінеті; refresh — суто протокольний артефакт, яким не можна
    зробити жодного запиту до даних.

    Ротується при кожному використанні (`replaced_at`), як того вимагає
    MCP-специфікація для public-клієнтів.
    """

    __tablename__ = "oauth_refresh_tokens"
    __table_args__ = (
        UniqueConstraint("token_sha256", name="uq_oauth_refresh_tokens_token_sha256"),
    )

    id: Mapped[str] = mapped_column(GUID, primary_key=True, default=new_uuid)
    token_sha256: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    client_id: Mapped[str] = mapped_column(String(64), index=True)
    user_id: Mapped[str] = mapped_column(
        GUID, ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    scopes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)


class AuditEvent(Base):
    """Один рядок журналу «хто що коли зробив» (task-72).

    Append-only: жодного `UPDATE`/`DELETE` по окремому рядку немає ні в API,
    ні в репозиторії — єдиний легальний спосіб зникнути це ретенція.

    Актор денормалізований НАВМИСНО (`actor_email`/`actor_name`/`actor_role`
    — копії на момент дії). Через рік людина може бути перейменована,
    деактивована або мати іншу роль, а журнал має читатись так, як він
    виглядав тоді; JOIN на `users` перетворив би історію на «unknown».
    """

    __tablename__ = "audit_events"
    __table_args__ = (
        Index("ix_audit_events_ts", "ts"),
        Index("ix_audit_events_actor_id_ts", "actor_id", "ts"),
        Index("ix_audit_events_action_ts", "action", "ts"),
        Index("ix_audit_events_resource", "resource_type", "resource_id", "ts"),
    )

    id: Mapped[str] = mapped_column(GUID, primary_key=True, default=new_uuid)
    ts: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    # `user` | `system` | `cli` | `job` — рядок, а не enum: воркер і CLI
    # додають свої види дій без міграції.
    actor_kind: Mapped[str] = mapped_column(String(16), default="system")
    actor_id: Mapped[str | None] = mapped_column(String(64), default=None)
    actor_email: Mapped[str | None] = mapped_column(String(320), default=None)
    actor_name: Mapped[str | None] = mapped_column(String(200), default=None)
    actor_role: Mapped[str | None] = mapped_column(String(16), default=None)
    session_id: Mapped[str | None] = mapped_column(String(64), default=None)
    ip: Mapped[str | None] = mapped_column(String(64), default=None)
    user_agent: Mapped[str | None] = mapped_column(String(256), default=None)
    # Крапкова нотація з явної мапи (`app/audit/actions.py`): `client.create`,
    # `job.start`, `auth.login.failed`. Це те, що читає людина у фільтрі, тому
    # назва приходить із мапи, а не генерується зі шляху.
    action: Mapped[str] = mapped_column(String(80), index=True)
    resource_type: Mapped[str | None] = mapped_column(String(40), default=None)
    resource_id: Mapped[str | None] = mapped_column(String(64), default=None)
    # Людська назва ресурсу на момент дії — з тієї ж причини, що й актор:
    # клієнта могли перейменувати або видалити.
    resource_label: Mapped[str | None] = mapped_column(String(200), default=None)
    method: Mapped[str | None] = mapped_column(String(10), default=None)
    path: Mapped[str | None] = mapped_column(String(500), default=None)
    status_code: Mapped[int | None] = mapped_column(Integer, default=None)
    # `success` | `failure` | `denied`
    outcome: Mapped[str] = mapped_column(String(16), default="success")
    duration_ms: Mapped[int | None] = mapped_column(Integer, default=None)
    # Редагований зріз запиту (ключі з денилиста замінені на `***`), обрізаний
    # за розміром. Секрети сюди не потрапляють — інваріант PLAN §13 сильніший
    # за повноту журналу.
    details_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


def _reject_mutation_except(allowed: set[str]) -> Any:
    """Same intent as `_reject_update`, relaxed for a small allow-list of
    fields (task-59's `ArchiveSnapshot`/`ArchiveAssetVersion`, which stay
    "immutable" except for recording that a later capture reconfirmed the
    same content). A local `inspect` import keeps this purely additive at
    the bottom of the file instead of touching the shared import block at
    the top, which other in-flight sessions may be editing concurrently.
    """

    def _check(mapper: Any, connection: Any, target: Base) -> None:
        from sqlalchemy import inspect as _inspect

        state = _inspect(target)
        for attr in state.attrs:
            if attr.key in allowed:
                continue
            if attr.load_history().has_changes():
                raise ValueError(
                    f"{type(target).__name__}.{attr.key} is immutable after commit"
                )

    return _check


event.listen(ProviderSnapshot, "before_update", _reject_update)
event.listen(MetricValue, "before_update", _reject_update)
event.listen(GscDimensionMetric, "before_update", _reject_update)
event.listen(MonitorResourceVersion, "before_update", _reject_update)
event.listen(MonitorPageVersion, "before_update", _reject_update)
event.listen(MonitorDnsVersion, "before_update", _reject_update)
event.listen(
    ArchiveSnapshot,
    "before_update",
    _reject_mutation_except({"last_confirmed_at", "confirmed_count"}),
)
event.listen(
    ArchiveAssetVersion, "before_update", _reject_mutation_except({"last_confirmed_at"})
)
# Журнал — append-only (task-72): рядок не редагують ніколи. Той самий
# запобіжник, що стоїть на снапшотах і метриках вище.
event.listen(AuditEvent, "before_update", _reject_update)
