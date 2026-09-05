"""Web Archive repository (task-59): the only place that touches
`archive_*` tables directly.

Grouped by concern, same "current state + immutable version chain" shape
`app.db.repositories.monitor` uses: a snapshot/asset-version write only ever
happens when the caller (`app.webarchive.capture`) already decided the
content actually changed; an unchanged capture instead calls
`confirm_snapshot`/`confirm_asset_version`, which touch only the two fields
the model's own `before_update` guard allow-lists.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from app.db.base import utcnow
from app.db.models import (
    ArchiveAsset,
    ArchiveAssetVersion,
    ArchiveBlob,
    ArchiveCapture,
    ArchiveCaptureEvent,
    ArchiveChange,
    ArchiveReplayMiss,
    ArchiveSettings,
    ArchiveSnapshot,
    ArchiveSnapshotAsset,
    ArchiveUrl,
    Client,
    MonitorPage,
)

CaptureStatus = Literal["queued", "running", "ok", "partial", "failed", "blocked", "cancelled"]


class WebArchiveRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def commit(self) -> None:
        self._session.commit()

    def flush(self) -> None:
        self._session.flush()

    # --- settings ------------------------------------------------------

    def get_settings(self, client_id: str) -> ArchiveSettings | None:
        stmt = select(ArchiveSettings).where(ArchiveSettings.client_id == client_id)
        return self._session.scalars(stmt).first()

    def get_or_create_settings(self, client_id: str) -> ArchiveSettings:
        settings = self.get_settings(client_id)
        if settings is not None:
            return settings
        settings = ArchiveSettings(client_id=client_id)
        self._session.add(settings)
        self._session.flush()
        return settings

    def all_settings_by_client(self) -> dict[str, ArchiveSettings]:
        rows = self._session.scalars(select(ArchiveSettings))
        return {row.client_id: row for row in rows}

    def update_settings(self, client_id: str, **fields: Any) -> ArchiveSettings:
        settings = self.get_or_create_settings(client_id)
        for key, value in fields.items():
            if value is not None:
                setattr(settings, key, value)
        settings.updated_at = utcnow()
        self._session.flush()
        return settings

    # --- captures --------------------------------------------------------

    def get_capture_by_slot(self, client_id: str, slot_key: str) -> ArchiveCapture | None:
        stmt = select(ArchiveCapture).where(
            ArchiveCapture.client_id == client_id, ArchiveCapture.slot_key == slot_key
        )
        return self._session.scalars(stmt).first()

    def create_capture(
        self,
        client_id: str,
        *,
        slot_key: str,
        scheduled_for: datetime,
        trigger: str,
        scope: str = "client",
        archive_url_id: str | None = None,
        now: datetime | None = None,
    ) -> ArchiveCapture:
        capture = ArchiveCapture(
            client_id=client_id,
            slot_key=slot_key,
            scheduled_for=scheduled_for,
            trigger=trigger,
            status="queued",
            queued_at=now or utcnow(),
            scope=scope,
            archive_url_id=archive_url_id,
        )
        self._session.add(capture)
        self._session.flush()
        return capture

    def get_capture(self, capture_id: str) -> ArchiveCapture | None:
        return self._session.get(ArchiveCapture, capture_id)

    def queued_captures(self, *, limit: int | None = None) -> list[ArchiveCapture]:
        """FIFO order (task-62 §1.3) — no priority class: a manual "Refresh
        now" and a scheduled slot both just join the tail.
        """
        stmt = select(ArchiveCapture).where(ArchiveCapture.status == "queued")
        stmt = stmt.order_by(ArchiveCapture.queued_at.asc())
        if limit is not None:
            stmt = stmt.limit(limit)
        return list(self._session.scalars(stmt))

    def running_captures(self) -> list[ArchiveCapture]:
        stmt = select(ArchiveCapture).where(ArchiveCapture.status == "running")
        return list(self._session.scalars(stmt))

    def has_running_capture(self, client_id: str) -> bool:
        stmt = select(ArchiveCapture.id).where(
            ArchiveCapture.client_id == client_id, ArchiveCapture.status == "running"
        )
        return self._session.scalars(stmt).first() is not None

    def has_active_capture(self, client_id: str) -> bool:
        """`running` OR `queued` — the admission check a NEW `POST /captures`
        must use (task-62 §1.3): guarding only `running` let two rapid
        clicks queue two rows for the same client before the scheduler's
        next tick claimed the first one.
        """
        stmt = select(ArchiveCapture.id).where(
            ArchiveCapture.client_id == client_id,
            ArchiveCapture.status.in_(("running", "queued")),
        )
        return self._session.scalars(stmt).first() is not None

    def claim_capture(
        self, capture_id: str, *, owner: str, now: datetime | None = None
    ) -> ArchiveCapture | None:
        """Only ever called from inside an `ArchiveWriteController` job
        (task-62 §2.2/§2.3): since that controller guarantees no OTHER
        archive write is in flight anywhere in this process while this
        function runs, the "is a capture already running for this client"
        check below and the `status="running"` write it guards are
        effectively atomic — the same admission-control-at-claim-time
        `cells.py:28 claim_next_cell` uses, just enforced by the
        controller's own serialization instead of a raw atomic `UPDATE`.
        """
        capture = self._session.get(ArchiveCapture, capture_id)
        if capture is None or capture.status != "queued":
            return None
        if self.has_running_capture(capture.client_id):
            return None
        moment = now or utcnow()
        capture.status = "running"
        capture.started_at = moment
        capture.owner = owner
        capture.heartbeat_at = moment
        self._session.flush()
        return capture

    def update_capture_counters(self, capture_id: str, **fields: Any) -> None:
        """Generic setattr over any `ArchiveCapture` column — counters,
        `heartbeat_at`, `progress_message` all ride the SAME periodic flush
        (task-62 §2.3/§2.5), so this adds no new commit frequency.
        """
        capture = self._session.get(ArchiveCapture, capture_id)
        if capture is None:
            return
        for key, value in fields.items():
            setattr(capture, key, value)
        self._session.flush()

    def request_cancel(self, capture_id: str, *, now: datetime | None = None) -> str | None:
        """`None` if the capture doesn't exist or is already terminal.
        A `queued` row is finished immediately (it never started, nothing to
        stop mid-flight). A `running` row is only flagged — `run_client_capture`
        checks `cancel_requested` between URLs and stops itself there
        (task-62 §1.6); the row stays `running` until it does.
        """
        capture = self._session.get(ArchiveCapture, capture_id)
        if capture is None:
            return None
        if capture.status == "queued":
            capture.status = "cancelled"
            capture.finished_at = now or utcnow()
            self._session.flush()
            return "cancelled"
        if capture.status == "running":
            capture.cancel_requested = True
            self._session.flush()
            return "cancel_requested"
        return None

    def finish_capture(
        self,
        capture_id: str,
        *,
        status: CaptureStatus,
        error_code: str | None = None,
        error_message: str | None = None,
        now: datetime | None = None,
    ) -> None:
        capture = self._session.get(ArchiveCapture, capture_id)
        if capture is None:
            return
        capture.status = status
        capture.finished_at = now or utcnow()
        capture.error_code = error_code
        capture.error_message = error_message
        self._session.flush()

    def list_captures(
        self, client_id: str | None = None, *, limit: int = 50
    ) -> list[ArchiveCapture]:
        stmt: Select[Any] = select(ArchiveCapture)
        if client_id is not None:
            stmt = stmt.where(ArchiveCapture.client_id == client_id)
        stmt = stmt.order_by(ArchiveCapture.scheduled_for.desc()).limit(limit)
        return list(self._session.scalars(stmt))

    def append_event(
        self,
        capture_id: str,
        level: str,
        *,
        stage: str | None = None,
        message: str,
        details: dict[str, Any] | None = None,
        now: datetime | None = None,
    ) -> int:
        """Same shape as `ReportRunRepository.append_event` — `seq` monotonic
        WITHIN this capture (task-62 §2.4). `run_client_capture` had no log
        at all before this (task-62 §0.5).
        """
        last_seq = self._session.scalar(
            select(func.max(ArchiveCaptureEvent.seq)).where(
                ArchiveCaptureEvent.capture_id == capture_id
            )
        )
        next_seq = (last_seq or 0) + 1
        self._session.add(
            ArchiveCaptureEvent(
                capture_id=capture_id,
                seq=next_seq,
                level=level,
                stage=stage,
                message=message,
                details_json=details or {},
                created_at=now or utcnow(),
            )
        )
        self._session.flush()
        return next_seq

    def events_since(self, capture_id: str, last_seq: int) -> list[ArchiveCaptureEvent]:
        stmt = (
            select(ArchiveCaptureEvent)
            .where(
                ArchiveCaptureEvent.capture_id == capture_id,
                ArchiveCaptureEvent.seq > last_seq,
            )
            .order_by(ArchiveCaptureEvent.seq.asc())
        )
        return list(self._session.scalars(stmt))

    def captures_on_date(self, client_id: str, day: datetime) -> list[ArchiveCapture]:
        start = datetime(day.year, day.month, day.day, tzinfo=UTC)
        end = start + timedelta(days=1)
        stmt = (
            select(ArchiveCapture)
            .where(
                ArchiveCapture.client_id == client_id,
                ArchiveCapture.scheduled_for >= start,
                ArchiveCapture.scheduled_for < end,
            )
            .order_by(ArchiveCapture.scheduled_for)
        )
        return list(self._session.scalars(stmt))

    # --- urls --------------------------------------------------------------

    def find_url(self, client_id: str, url_key: str) -> ArchiveUrl | None:
        stmt = select(ArchiveUrl).where(
            ArchiveUrl.client_id == client_id, ArchiveUrl.url_key == url_key
        )
        return self._session.scalars(stmt).first()

    def find_url_any_client(self, url_key: str) -> ArchiveUrl | None:
        stmt = select(ArchiveUrl).where(ArchiveUrl.url_key == url_key)
        return self._session.scalars(stmt).first()

    def get_url(self, url_id: str) -> ArchiveUrl | None:
        return self._session.get(ArchiveUrl, url_id)

    def get_or_create_url(
        self, client_id: str, url: str, url_key: str, *, source: str = "sitemap"
    ) -> ArchiveUrl:
        existing = self.find_url(client_id, url_key)
        if existing is not None:
            return existing
        archive_url = ArchiveUrl(client_id=client_id, url=url, url_key=url_key, source=source)
        self._session.add(archive_url)
        self._session.flush()
        return archive_url

    def list_urls_for_client(self, client_id: str, *, limit: int = 25_000) -> list[ArchiveUrl]:
        stmt = (
            select(ArchiveUrl)
            .where(ArchiveUrl.client_id == client_id)
            .order_by(ArchiveUrl.created_at)
            .limit(limit)
        )
        return list(self._session.scalars(stmt))

    def count_urls_for_client(self, client_id: str) -> int:
        stmt = select(func.count()).select_from(ArchiveUrl).where(ArchiveUrl.client_id == client_id)
        return int(self._session.scalar(stmt) or 0)

    def search_urls(
        self, *, client_id: str | None, query: str | None, page: int, page_size: int
    ) -> tuple[list[ArchiveUrl], int]:
        stmt: Select[Any] = select(ArchiveUrl)
        count_stmt: Select[Any] = select(func.count()).select_from(ArchiveUrl)
        if client_id is not None:
            stmt = stmt.where(ArchiveUrl.client_id == client_id)
            count_stmt = count_stmt.where(ArchiveUrl.client_id == client_id)
        if query:
            like = f"%{query.lower()}%"
            stmt = stmt.where(func.lower(ArchiveUrl.url).like(like))
            count_stmt = count_stmt.where(func.lower(ArchiveUrl.url).like(like))
        total = int(self._session.scalar(count_stmt) or 0)
        stmt = (
            stmt.order_by(ArchiveUrl.last_captured_at.desc().nulls_last())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        return list(self._session.scalars(stmt)), total

    def top_changing_urls(self, *, limit: int = 5) -> list[ArchiveUrl]:
        stmt = (
            select(ArchiveUrl)
            .where(ArchiveUrl.snapshot_count > 1)
            .order_by(ArchiveUrl.snapshot_count.desc())
            .limit(limit)
        )
        return list(self._session.scalars(stmt))

    def touch_url(
        self, archive_url: ArchiveUrl, *, observed_at: datetime, snapshot_written: bool
    ) -> None:
        if archive_url.first_captured_at is None:
            archive_url.first_captured_at = observed_at
        archive_url.last_captured_at = observed_at
        if snapshot_written:
            archive_url.snapshot_count += 1
        archive_url.gone_since = None
        self._session.flush()

    def mark_url_gone(self, archive_url: ArchiveUrl, *, observed_at: datetime) -> None:
        if archive_url.gone_since is None:
            archive_url.gone_since = observed_at
        self._session.flush()

    def delete_url(self, archive_url: ArchiveUrl) -> None:
        """Removes a tracked URL that turned out never to be a page (task-59,
        2026-09-04) — cascades to its snapshots/changes via the FK's own
        `ondelete="CASCADE"`.
        """
        self._session.delete(archive_url)
        self._session.flush()

    # --- snapshots -----------------------------------------------------

    def latest_snapshot(self, archive_url_id: str) -> ArchiveSnapshot | None:
        stmt = (
            select(ArchiveSnapshot)
            .where(ArchiveSnapshot.archive_url_id == archive_url_id)
            .order_by(ArchiveSnapshot.observed_at.desc())
            .limit(1)
        )
        return self._session.scalars(stmt).first()

    def get_snapshot(self, snapshot_id: str) -> ArchiveSnapshot | None:
        return self._session.get(ArchiveSnapshot, snapshot_id)

    def create_snapshot(self, **fields: Any) -> ArchiveSnapshot:
        snapshot = ArchiveSnapshot(**fields)
        self._session.add(snapshot)
        self._session.flush()
        return snapshot

    def confirm_snapshot(self, snapshot: ArchiveSnapshot, *, observed_at: datetime) -> None:
        snapshot.last_confirmed_at = observed_at
        snapshot.confirmed_count += 1
        self._session.flush()

    def list_snapshots_for_url(self, archive_url_id: str) -> list[ArchiveSnapshot]:
        stmt = (
            select(ArchiveSnapshot)
            .where(ArchiveSnapshot.archive_url_id == archive_url_id)
            .order_by(ArchiveSnapshot.observed_at.desc())
        )
        return list(self._session.scalars(stmt))

    def resolve_snapshot(
        self, archive_url_id: str, at: datetime
    ) -> tuple[ArchiveSnapshot | None, bool]:
        """The version live AT `at`: the most recent snapshot with
        `observed_at <= at`. When none exists (asking for a moment before
        the URL was ever captured), falls back to the EARLIEST snapshot with
        `is_exact=False` — the closest available in the only direction there
        is, same "nearest capture" convention Wayback itself uses.

        `at` is a 14-digit `ts` (`app.webarchive.replay.parse_ts`) — SECOND
        precision — while `observed_at` carries microseconds. A snapshot's
        OWN `ts` is therefore always a whole second earlier than its own
        `observed_at`; a literal `observed_at <= at` excludes a snapshot
        from resolving to ITSELF (task-63 §2.1, live-confirmed: every one of
        4 versions of a real multi-version URL resolved one version stale).
        Widening the upper bound to the next whole second is exact, not an
        approximation: two snapshots of the same URL within one second of
        each other cannot happen (capture is sequential per URL, median
        5.8s/URL — task-62 §0).
        """
        stmt = (
            select(ArchiveSnapshot)
            .where(
                ArchiveSnapshot.archive_url_id == archive_url_id,
                ArchiveSnapshot.observed_at < at + timedelta(seconds=1),
            )
            .order_by(ArchiveSnapshot.observed_at.desc())
            .limit(1)
        )
        snapshot = self._session.scalars(stmt).first()
        if snapshot is not None:
            return snapshot, True
        earliest_stmt = (
            select(ArchiveSnapshot)
            .where(ArchiveSnapshot.archive_url_id == archive_url_id)
            .order_by(ArchiveSnapshot.observed_at.asc())
            .limit(1)
        )
        return self._session.scalars(earliest_stmt).first(), False

    def count_snapshots_for_client(self, client_id: str) -> int:
        stmt = (
            select(func.count()).select_from(ArchiveSnapshot)
            .where(ArchiveSnapshot.client_id == client_id)
        )
        return int(self._session.scalar(stmt) or 0)

    def count_snapshots_total(self) -> int:
        return int(self._session.scalar(select(func.count()).select_from(ArchiveSnapshot)) or 0)

    def all_snapshots(self, client_id: str | None = None) -> list[ArchiveSnapshot]:
        """Every stored snapshot, optionally scoped to one client — used by
        `backfill_css_assets` (task-60 §1.3) to re-scan already-captured
        HTML for CSS-only references the discovery fix now catches but an
        earlier capture pass missed. Not a hot path: a one-off CLI sweep,
        not something called during a normal capture or replay.
        """
        stmt: Select[Any] = select(ArchiveSnapshot)
        if client_id is not None:
            stmt = stmt.where(ArchiveSnapshot.client_id == client_id)
        return list(self._session.scalars(stmt))

    # --- assets ----------------------------------------------------------

    def get_asset_by_key(self, url_key: str) -> ArchiveAsset | None:
        stmt = select(ArchiveAsset).where(ArchiveAsset.url_key == url_key)
        return self._session.scalars(stmt).first()

    def find_asset_by_key_prefix(self, prefix: str) -> ArchiveAsset | None:
        """Best-effort recovery for a query-string cache-buster mismatch
        (task-63 §3.2, live-measured: 59 distinct replay misses carry a
        query string, e.g. `?v=<timestamp>` a page's own JS appends at
        request time). The bytes behind a matching key were never fetched
        over the network for THIS request — this only ever hands back a
        stored, already-archived version of the SAME file at a different
        point in its own revision history, never a different resource.
        `%`/`_` in `prefix` (both valid in a real URL path) are escaped so
        they match literally, not as SQL `LIKE` wildcards.
        """
        escaped = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        stmt = (
            select(ArchiveAsset)
            .where(ArchiveAsset.url_key.like(f"{escaped}%", escape="\\"))
            .order_by(ArchiveAsset.first_seen_at.desc())
            .limit(1)
        )
        return self._session.scalars(stmt).first()

    def get_or_create_asset(self, url: str, url_key: str, *, kind: str) -> ArchiveAsset:
        existing = self.get_asset_by_key(url_key)
        if existing is not None:
            return existing
        asset = ArchiveAsset(url=url, url_key=url_key, kind=kind)
        self._session.add(asset)
        self._session.flush()
        return asset

    def record_asset_check(
        self,
        asset: ArchiveAsset,
        *,
        checked_at: datetime,
        etag: str | None,
        last_modified: str | None,
        failed: bool,
    ) -> None:
        asset.last_checked_at = checked_at
        if etag is not None:
            asset.etag = etag
        if last_modified is not None:
            asset.last_modified = last_modified
        asset.failure_count = asset.failure_count + 1 if failed else 0
        self._session.flush()

    def latest_asset_version(self, asset_id: str) -> ArchiveAssetVersion | None:
        stmt = (
            select(ArchiveAssetVersion)
            .where(ArchiveAssetVersion.asset_id == asset_id)
            .order_by(ArchiveAssetVersion.first_seen_at.desc())
            .limit(1)
        )
        return self._session.scalars(stmt).first()

    def create_asset_version(self, **fields: Any) -> ArchiveAssetVersion:
        version = ArchiveAssetVersion(**fields)
        self._session.add(version)
        self._session.flush()
        return version

    def confirm_asset_version(self, version: ArchiveAssetVersion, *, at: datetime) -> None:
        version.last_confirmed_at = at
        self._session.flush()

    def resolve_asset_version(
        self, asset_id: str, at: datetime
    ) -> tuple[ArchiveAssetVersion | None, bool]:
        """Same second-vs-microsecond fix as `resolve_snapshot` above
        (task-63 §2.1) — `at` is always a whole-second `ts`, so a literal
        `<=` would exclude a version confirmed within the same second as the
        page's own snapshot.
        """
        stmt = (
            select(ArchiveAssetVersion)
            .where(
                ArchiveAssetVersion.asset_id == asset_id,
                ArchiveAssetVersion.first_seen_at < at + timedelta(seconds=1),
            )
            .order_by(ArchiveAssetVersion.first_seen_at.desc())
            .limit(1)
        )
        version = self._session.scalars(stmt).first()
        if version is not None:
            return version, True
        earliest_stmt = (
            select(ArchiveAssetVersion)
            .where(ArchiveAssetVersion.asset_id == asset_id)
            .order_by(ArchiveAssetVersion.first_seen_at.asc())
            .limit(1)
        )
        return self._session.scalars(earliest_stmt).first(), False

    def link_snapshot_asset(
        self,
        *,
        snapshot_id: str,
        asset_id: str,
        asset_version_id: str | None,
        role: str,
    ) -> None:
        self._session.add(
            ArchiveSnapshotAsset(
                snapshot_id=snapshot_id,
                asset_id=asset_id,
                asset_version_id=asset_version_id,
                role=role,
            )
        )

    def snapshot_assets(
        self, snapshot_id: str
    ) -> list[tuple[ArchiveSnapshotAsset, ArchiveAsset, ArchiveAssetVersion | None]]:
        stmt = (
            select(ArchiveSnapshotAsset, ArchiveAsset, ArchiveAssetVersion)
            .join(ArchiveAsset, ArchiveAsset.id == ArchiveSnapshotAsset.asset_id)
            .outerjoin(
                ArchiveAssetVersion, ArchiveAssetVersion.id == ArchiveSnapshotAsset.asset_version_id
            )
            .where(ArchiveSnapshotAsset.snapshot_id == snapshot_id)
        )
        return [tuple(row) for row in self._session.execute(stmt).all()]

    def all_css_assets(self) -> list[ArchiveAsset]:
        """Every tracked CSS file, portfolio-wide — `backfill_css_assets`
        (task-60 §1.3) reads each one's stored body for `url(...)`
        references a capture pass before the discovery fix never followed.
        """
        stmt = select(ArchiveAsset).where(ArchiveAsset.kind == "css")
        return list(self._session.scalars(stmt))

    def css_assets_for_client(self, client_id: str) -> list[ArchiveAsset]:
        """The CSS files ONE client's own captured pages reference —
        `ArchiveAsset` carries no `client_id` of its own (task-60: it is a
        global, content-addressed registry, potentially shared across
        clients), so scoping goes through the snapshots that link to it.
        """
        stmt = (
            select(ArchiveAsset)
            .join(ArchiveSnapshotAsset, ArchiveSnapshotAsset.asset_id == ArchiveAsset.id)
            .join(ArchiveSnapshot, ArchiveSnapshot.id == ArchiveSnapshotAsset.snapshot_id)
            .where(ArchiveSnapshot.client_id == client_id, ArchiveAsset.kind == "css")
            .distinct()
        )
        return list(self._session.scalars(stmt))

    def asset_coverage_totals(self) -> dict[str, int]:
        """Portfolio-wide `{"referenced": N, "captured": M}` for the hub's
        Asset coverage tile (task-60 §1.4) — "referenced" is every asset
        discovery has ever turned up (an `ArchiveAsset` row exists as soon
        as it is FIRST seen, fetch outcome notwithstanding); "captured" is
        the subset that actually has at least one stored version. The gap
        between them is either a genuinely-dead source-site file (honest —
        the archive never invents bytes) or a not-yet-run backfill.
        """
        referenced = int(self._session.scalar(select(func.count()).select_from(ArchiveAsset)) or 0)
        captured = int(
            self._session.scalar(
                select(func.count(func.distinct(ArchiveAssetVersion.asset_id)))
            )
            or 0
        )
        return {"referenced": referenced, "captured": captured}

    # --- blobs -----------------------------------------------------------

    def get_blob(self, sha256: str) -> ArchiveBlob | None:
        return self._session.get(ArchiveBlob, sha256)

    def register_blob(
        self,
        sha256: str,
        *,
        size_bytes: int,
        gzip_size_bytes: int,
        content_type: str | None,
        path: str,
    ) -> ArchiveBlob:
        existing = self.get_blob(sha256)
        if existing is not None:
            if content_type and not existing.content_type:
                existing.content_type = content_type
                self._session.flush()
            return existing
        blob = ArchiveBlob(
            sha256=sha256,
            size_bytes=size_bytes,
            gzip_size_bytes=gzip_size_bytes,
            content_type=content_type,
            path=path,
        )
        self._session.add(blob)
        self._session.flush()
        return blob

    def blobs_not_uploaded(self, *, limit: int = 200) -> list[ArchiveBlob]:
        stmt = (
            select(ArchiveBlob)
            .where(ArchiveBlob.uploaded_at.is_(None))
            .order_by(ArchiveBlob.first_seen_at)
            .limit(limit)
        )
        return list(self._session.scalars(stmt))

    def mark_uploaded(self, sha256: str, *, at: datetime | None = None) -> None:
        blob = self.get_blob(sha256)
        if blob is None:
            return
        blob.uploaded_at = at or utcnow()
        self._session.flush()

    def mark_local_deleted(self, sha256: str, *, at: datetime | None = None) -> None:
        blob = self.get_blob(sha256)
        if blob is None:
            return
        blob.local_deleted_at = at or utcnow()
        self._session.flush()

    def blob_stats(self) -> dict[str, int]:
        total = self._session.execute(
            select(func.count(), func.coalesce(func.sum(ArchiveBlob.gzip_size_bytes), 0))
        ).one()
        uploaded = self._session.execute(
            select(func.coalesce(func.sum(ArchiveBlob.gzip_size_bytes), 0)).where(
                ArchiveBlob.uploaded_at.is_not(None)
            )
        ).scalar_one()
        return {
            "blobs_total": int(total[0]),
            "bytes_local": int(total[1]),
            "bytes_uploaded": int(uploaded),
            "bytes_not_uploaded": int(total[1]) - int(uploaded),
        }

    # --- changes ---------------------------------------------------------

    def create_change(self, **fields: Any) -> ArchiveChange:
        change = ArchiveChange(**fields)
        self._session.add(change)
        self._session.flush()
        return change

    def has_change_for_snapshot(self, snapshot_id: str) -> bool:
        stmt = select(ArchiveChange.id).where(ArchiveChange.snapshot_id == snapshot_id)
        return self._session.scalars(stmt).first() is not None

    def get_change_by_snapshot(self, snapshot_id: str) -> ArchiveChange | None:
        stmt = select(ArchiveChange).where(ArchiveChange.snapshot_id == snapshot_id)
        return self._session.scalars(stmt).first()

    def list_changes(
        self,
        *,
        client_id: str | None,
        kind: str | None,
        start: datetime | None,
        end: datetime | None,
        page: int,
        page_size: int,
    ) -> tuple[list[ArchiveChange], int]:
        stmt: Select[Any] = select(ArchiveChange)
        count_stmt: Select[Any] = select(func.count()).select_from(ArchiveChange)
        if client_id is not None:
            stmt = stmt.where(ArchiveChange.client_id == client_id)
            count_stmt = count_stmt.where(ArchiveChange.client_id == client_id)
        if start is not None:
            stmt = stmt.where(ArchiveChange.detected_at >= start)
            count_stmt = count_stmt.where(ArchiveChange.detected_at >= start)
        if end is not None:
            stmt = stmt.where(ArchiveChange.detected_at <= end)
            count_stmt = count_stmt.where(ArchiveChange.detected_at <= end)
        if kind is not None:
            # SQLite JSON: a plain LIKE over the serialized array is enough
            # here (small per-row array, filter is a UI convenience, not a
            # security boundary) — mirrors how `MonitorChange` filters are
            # done elsewhere in this codebase.
            like = f'%"{kind}"%'
            stmt = stmt.where(ArchiveChange.change_kinds_json.cast(str).like(like))
            count_stmt = count_stmt.where(ArchiveChange.change_kinds_json.cast(str).like(like))
        total = int(self._session.scalar(count_stmt) or 0)
        stmt = (
            stmt.order_by(ArchiveChange.detected_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        return list(self._session.scalars(stmt)), total

    # --- replay misses -----------------------------------------------------

    def record_replay_miss(self, url: str, url_key: str, *, now: datetime | None = None) -> None:
        now = now or utcnow()
        stmt = select(ArchiveReplayMiss).where(ArchiveReplayMiss.url_key == url_key)
        existing = self._session.scalars(stmt).first()
        if existing is not None:
            existing.hit_count += 1
            existing.last_seen_at = now
        else:
            self._session.add(
                ArchiveReplayMiss(
                    url=url, url_key=url_key, hit_count=1,
                    first_seen_at=now, last_seen_at=now,
                )
            )
        self._session.flush()

    def count_replay_misses_for_host(self, host: str) -> int:
        """Rows in `archive_replay_misses` whose `url_key` starts with this
        host (task-63 §3.5). Scoped to the HOST, not one exact page — a
        miss row has no link back to which page's replay triggered it (no
        migration adds one here), so this is the closest honest signal the
        Meta panel can show without inventing a false precision.
        """
        stmt = (
            select(func.count()).select_from(ArchiveReplayMiss)
            .where(ArchiveReplayMiss.url_key.like(f"{host}/%"))
        )
        return int(self._session.scalar(stmt) or 0)

    # --- cross-reference into Site Monitor (read-only) --------------------

    def monitor_known_url_count(self, client_id: str) -> int:
        """How many URLs the monitor already tracks for this client — used
        only to render a "coverage" number on the archive hub (task-59
        §1.9); never written to, never joined against for capture logic.
        """
        stmt = (
            select(func.count()).select_from(MonitorPage)
            .where(MonitorPage.client_id == client_id)
        )
        return int(self._session.scalar(stmt) or 0)

    def client_name(self, client_id: str) -> str | None:
        return self._session.scalar(select(Client.name).where(Client.id == client_id))

    def clients_by_id(self, client_ids: Sequence[str]) -> dict[str, Client]:
        if not client_ids:
            return {}
        stmt = select(Client).where(Client.id.in_(client_ids))
        return {client.id: client for client in self._session.scalars(stmt)}
