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
    # `enabled` — технічна видимість рядка: `False` ховає клієнта з дашборду
    # (`ClientRepository.list_active`). Це НЕ «пауза».
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
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
    description: Mapped[str | None] = mapped_column(String(1000), default=None)
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


# --- Added by task-42 (Reports engine, PLAN §16 "Хвиля N") -----------------
#
# `kind`/`source`/`status`/`cross_check_status` are plain `String`, not
# `sa.Enum` — the same choice task-35 already made for monitor's own
# tool-level states: adding a report kind or a new source must not require
# DDL. Reports are deliberately NOT a `SyncJob`/provider (task-42 §2.2): no
# `(client, provider)` cell, no metrics catalog, no browser profile lock —
# just a client, a parameter set and a list of output files.


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
    source: Mapped[str] = mapped_column(String(8))
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


class ReportRun(Base):
    """One report generation run (task-42 §4). Mutable while `status` is
    `queued`/`running` (counters update live, mirrors `SyncJob.progress_percent`
    and `MonitorRun`), then left alone once finished.
    """

    __tablename__ = "report_runs"
    __table_args__ = (
        Index("ix_report_runs_client_created", "client_id", "created_at"),
        Index("ix_report_runs_status", "status"),
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
    source: Mapped[str] = mapped_column(String(8))  # api|pdf
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


def _reject_update(mapper: Any, connection: Any, target: Base) -> None:
    raise ValueError(f"{type(target).__name__} rows are immutable after commit")


event.listen(ProviderSnapshot, "before_update", _reject_update)
event.listen(MetricValue, "before_update", _reject_update)
event.listen(GscDimensionMetric, "before_update", _reject_update)
event.listen(MonitorResourceVersion, "before_update", _reject_update)
event.listen(MonitorPageVersion, "before_update", _reject_update)
event.listen(MonitorDnsVersion, "before_update", _reject_update)
