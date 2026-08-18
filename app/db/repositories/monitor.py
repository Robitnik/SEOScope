"""Site Monitor repository (task-35): the only place that touches
`monitor_*` tables directly.

Grouped by the "current state + immutable version chain" shape used
throughout (task-35 §3.2): a `*_state` write upserts the mutable row and
bumps `last_checked_at`; a `*_version` write appends a new immutable row and
is only ever called when the caller (app/monitor/diff.py) already decided the
fingerprint changed.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any, Literal

from sqlalchemy import Select, and_, func, or_, select
from sqlalchemy.orm import Session

from app.db.base import utcnow
from app.db.models import (
    MonitorBlob,
    MonitorChange,
    MonitorDnsRecord,
    MonitorDnsVersion,
    MonitorPage,
    MonitorPageVersion,
    MonitorResource,
    MonitorResourceVersion,
    MonitorRun,
    MonitorSettings,
)

ChangeState = Literal["open", "all"]


class MonitorRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def commit(self) -> None:
        """Callers (`app/monitor/runner.py`) hold a process-wide lock across
        every write AND this commit as one unit — SQLite's file-level write
        lock is only released on COMMIT, so calling this outside that lock
        would let another thread's flush collide with a still-open,
        uncommitted transaction (task-35 §3.6, live-measured 2026-08-11)."""
        self._session.commit()

    # --- settings ------------------------------------------------------

    def get_settings(self, client_id: str) -> MonitorSettings | None:
        stmt = select(MonitorSettings).where(MonitorSettings.client_id == client_id)
        return self._session.scalars(stmt).first()

    def get_or_create_settings(self, client_id: str) -> MonitorSettings:
        settings = self.get_settings(client_id)
        if settings is not None:
            return settings
        settings = MonitorSettings(client_id=client_id)
        self._session.add(settings)
        self._session.flush()
        return settings

    def update_settings(self, client_id: str, **fields: Any) -> MonitorSettings:
        settings = self.get_or_create_settings(client_id)
        for key, value in fields.items():
            if value is not None or key in {"base_url_override", "canonical_www"}:
                setattr(settings, key, value)
        settings.updated_at = utcnow()
        self._session.flush()
        return settings

    def all_settings_by_client(self) -> dict[str, MonitorSettings]:
        rows = self._session.scalars(select(MonitorSettings))
        return {row.client_id: row for row in rows}

    # --- runs ------------------------------------------------------------

    def create_run(self, client_id: str, *, kind: str, trigger: str = "scheduled") -> MonitorRun:
        run = MonitorRun(client_id=client_id, kind=kind, trigger=trigger, status="running")
        self._session.add(run)
        self._session.flush()
        return run

    def enqueue_run(self, client_id: str, *, kind: str, trigger: str = "manual") -> MonitorRun:
        """`status="queued"` — written by the API's "Check now" (fast, no
        crawling happens in the API process); the worker's scheduler tick
        claims it via `claim_queued_run` and hands it to `run_client_monitor`
        (same enqueue/claim split as `SyncJob`).
        """
        run = MonitorRun(client_id=client_id, kind=kind, trigger=trigger, status="queued")
        self._session.add(run)
        self._session.flush()
        return run

    def queued_runs(self) -> list[MonitorRun]:
        return list(self._session.scalars(select(MonitorRun).where(MonitorRun.status == "queued")))

    def claim_queued_run(self, run_id: str, *, now: datetime | None = None) -> MonitorRun | None:
        run = self._session.get(MonitorRun, run_id)
        if run is None or run.status != "queued":
            return None
        run.status = "running"
        run.started_at = now or utcnow()
        self._session.flush()
        return run

    def get_run(self, run_id: str) -> MonitorRun | None:
        return self._session.get(MonitorRun, run_id)

    def update_run_counters(self, run_id: str, **counters: int) -> None:
        run = self._session.get(MonitorRun, run_id)
        if run is None:
            return
        for key, value in counters.items():
            setattr(run, key, value)

    def finish_run(
        self,
        run_id: str,
        *,
        status: str,
        truncated: bool = False,
        remaining_count: int | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        notes: dict[str, Any] | None = None,
    ) -> MonitorRun | None:
        run = self._session.get(MonitorRun, run_id)
        if run is None:
            return None
        run.status = status
        run.finished_at = utcnow()
        run.truncated = truncated
        run.remaining_count = remaining_count
        run.error_code = error_code
        run.error_message = error_message
        if notes is not None:
            run.notes_json = notes
        self._session.flush()
        return run

    def latest_run(self, client_id: str, *, kind: str | None = None) -> MonitorRun | None:
        stmt = select(MonitorRun).where(MonitorRun.client_id == client_id)
        if kind is not None:
            stmt = stmt.where(MonitorRun.kind == kind)
        stmt = stmt.order_by(MonitorRun.started_at.desc()).limit(1)
        return self._session.scalars(stmt).first()

    def latest_finished_run(self, client_id: str, *, kind: str | None = None) -> MonitorRun | None:
        stmt = select(MonitorRun).where(
            MonitorRun.client_id == client_id,
            MonitorRun.status.not_in(("running", "queued")),
        )
        if kind is not None:
            stmt = stmt.where(MonitorRun.kind == kind)
        stmt = stmt.order_by(MonitorRun.started_at.desc()).limit(1)
        return self._session.scalars(stmt).first()

    def list_runs(
        self, client_id: str, *, kind: str | None = None, limit: int = 50
    ) -> list[MonitorRun]:
        stmt = select(MonitorRun).where(MonitorRun.client_id == client_id)
        if kind is not None:
            stmt = stmt.where(MonitorRun.kind == kind)
        stmt = stmt.order_by(MonitorRun.started_at.desc()).limit(limit)
        return list(self._session.scalars(stmt))

    def running_runs(self) -> list[MonitorRun]:
        return list(self._session.scalars(select(MonitorRun).where(MonitorRun.status == "running")))

    def has_running_run(self, client_id: str, *, kind: str | None = None) -> bool:
        """`True` for `running` *or* `queued` — a manual "Check now" not yet
        claimed by the scheduler still counts as "already in flight" so the
        next tick does not also queue a duplicate scheduled run.
        """
        stmt = (
            select(func.count())
            .select_from(MonitorRun)
            .where(MonitorRun.client_id == client_id, MonitorRun.status.in_(("running", "queued")))
        )
        if kind is not None:
            stmt = stmt.where(MonitorRun.kind == kind)
        return (self._session.scalar(stmt) or 0) > 0

    # --- blobs (content-addressed) ---------------------------------------

    def get_blob(self, sha256: str) -> MonitorBlob | None:
        return self._session.get(MonitorBlob, sha256)

    def touch_blob(self, sha256: str) -> None:
        blob = self._session.get(MonitorBlob, sha256)
        if blob is not None:
            blob.last_seen_at = utcnow()

    def record_blob(
        self, sha256: str, *, size_bytes: int, gzip_size_bytes: int, mime: str | None, path: str
    ) -> MonitorBlob:
        existing = self._session.get(MonitorBlob, sha256)
        if existing is not None:
            existing.last_seen_at = utcnow()
            return existing
        blob = MonitorBlob(
            sha256=sha256,
            size_bytes=size_bytes,
            gzip_size_bytes=gzip_size_bytes,
            mime=mime,
            path=path,
        )
        self._session.add(blob)
        self._session.flush()
        return blob

    def referenced_blob_sha256s(self) -> set[str]:
        page_refs = self._session.scalars(
            select(MonitorPageVersion.body_blob_sha256).where(
                MonitorPageVersion.body_blob_sha256.is_not(None)
            )
        )
        resource_refs = self._session.scalars(
            select(MonitorResourceVersion.content_sha256).where(
                MonitorResourceVersion.content_sha256.is_not(None)
            )
        )
        return {*page_refs, *resource_refs}

    def unreferenced_blobs(self) -> list[MonitorBlob]:
        referenced = self.referenced_blob_sha256s()
        all_blobs = list(self._session.scalars(select(MonitorBlob)))
        return [blob for blob in all_blobs if blob.sha256 not in referenced]

    def delete_blob(self, sha256: str) -> None:
        """Deleting the row alone is enough to detach it from every version
        that pointed at it: `body_blob_sha256`/`content_sha256` carry
        `ondelete="SET NULL"`, enforced by SQLite itself on the `DELETE`
        statement — not an ORM `UPDATE`, so it never touches the
        `before_update` immutability guard on `MonitorPageVersion`/
        `MonitorResourceVersion` (task-35 §3.2/§3.9).
        """
        blob = self._session.get(MonitorBlob, sha256)
        if blob is not None:
            self._session.delete(blob)

    def blobs_referenced_only_by_versions_older_than(self, cutoff: datetime) -> list[str]:
        """sha256s whose every referencing version (page or resource) is
        older than `cutoff` — i.e. the blob is not the live body of any
        current page. Explicit `--bodies-older-than` retention only (task-35
        §3.2.1/§3.9); a blob still backing a recent, unchanged page's latest
        version is never a candidate, no matter how old that version is.
        """
        old_page = set(
            self._session.scalars(
                select(MonitorPageVersion.body_blob_sha256).where(
                    MonitorPageVersion.body_blob_sha256.is_not(None),
                    MonitorPageVersion.observed_at < cutoff,
                )
            )
        )
        fresh_page = set(
            self._session.scalars(
                select(MonitorPageVersion.body_blob_sha256).where(
                    MonitorPageVersion.body_blob_sha256.is_not(None),
                    MonitorPageVersion.observed_at >= cutoff,
                )
            )
        )
        old_resource = set(
            self._session.scalars(
                select(MonitorResourceVersion.content_sha256).where(
                    MonitorResourceVersion.content_sha256.is_not(None),
                    MonitorResourceVersion.observed_at < cutoff,
                )
            )
        )
        fresh_resource = set(
            self._session.scalars(
                select(MonitorResourceVersion.content_sha256).where(
                    MonitorResourceVersion.content_sha256.is_not(None),
                    MonitorResourceVersion.observed_at >= cutoff,
                )
            )
        )
        candidates = (old_page | old_resource) - (fresh_page | fresh_resource)
        return sorted(sha256 for sha256 in candidates if sha256 is not None)

    # --- resources (robots.txt / sitemap files) --------------------------

    def get_resource(self, client_id: str, url: str) -> MonitorResource | None:
        stmt = select(MonitorResource).where(
            MonitorResource.client_id == client_id, MonitorResource.url == url
        )
        return self._session.scalars(stmt).first()

    def list_resources(self, client_id: str, *, kind: str | None = None) -> list[MonitorResource]:
        stmt = select(MonitorResource).where(MonitorResource.client_id == client_id)
        if kind is not None:
            stmt = stmt.where(MonitorResource.kind == kind)
        return list(self._session.scalars(stmt.order_by(MonitorResource.url)))

    def upsert_resource_state(
        self,
        client_id: str,
        url: str,
        *,
        kind: str,
        status: int | None,
        content_sha256: str | None,
        structure: dict[str, Any],
        missing: bool,
        now: datetime | None = None,
    ) -> MonitorResource:
        now = now or utcnow()
        resource = self.get_resource(client_id, url)
        if resource is None:
            resource = MonitorResource(client_id=client_id, url=url, kind=kind)
            self._session.add(resource)
        resource.status = status
        resource.content_sha256 = content_sha256
        resource.structure_json = structure
        resource.last_checked_at = now
        if missing:
            resource.missing_since = resource.missing_since or now
        else:
            resource.missing_since = None
        self._session.flush()
        return resource

    def create_resource_version(
        self,
        resource: MonitorResource,
        *,
        http_status: int | None,
        content_sha256: str | None,
        structure: dict[str, Any],
        changed_fields: list[str],
        prev_version_id: str | None,
        observed_at: datetime | None = None,
    ) -> MonitorResourceVersion:
        version = MonitorResourceVersion(
            resource_id=resource.id,
            client_id=resource.client_id,
            observed_at=observed_at or utcnow(),
            http_status=http_status,
            content_sha256=content_sha256,
            structure_json=structure,
            changed_fields_json=changed_fields,
            prev_version_id=prev_version_id,
        )
        self._session.add(version)
        resource.version_seq += 1
        self._session.flush()
        return version

    def latest_resource_version(self, resource_id: str) -> MonitorResourceVersion | None:
        stmt = (
            select(MonitorResourceVersion)
            .where(MonitorResourceVersion.resource_id == resource_id)
            .order_by(MonitorResourceVersion.observed_at.desc())
            .limit(1)
        )
        return self._session.scalars(stmt).first()

    def resource_versions(
        self, resource_id: str, *, limit: int = 50
    ) -> list[MonitorResourceVersion]:
        stmt = (
            select(MonitorResourceVersion)
            .where(MonitorResourceVersion.resource_id == resource_id)
            .order_by(MonitorResourceVersion.observed_at.desc())
            .limit(limit)
        )
        return list(self._session.scalars(stmt))

    # --- pages -------------------------------------------------------------

    def get_page(self, client_id: str, url: str) -> MonitorPage | None:
        stmt = select(MonitorPage).where(MonitorPage.client_id == client_id, MonitorPage.url == url)
        return self._session.scalars(stmt).first()

    def get_page_by_id(self, page_id: str) -> MonitorPage | None:
        return self._session.get(MonitorPage, page_id)

    def all_pages(self, client_id: str) -> list[MonitorPage]:
        return list(
            self._session.scalars(select(MonitorPage).where(MonitorPage.client_id == client_id))
        )

    def pages_in_ring_order(self, client_id: str, *, key_only: bool = False) -> list[MonitorPage]:
        """Key pages + open-critical/warning first, then oldest-checked-first
        (task-35 §3.3 ring traversal) — used to pick up where a truncated run
        left off instead of re-checking the same head of the list forever.
        """
        stmt = select(MonitorPage).where(MonitorPage.client_id == client_id)
        if key_only:
            stmt = stmt.where(MonitorPage.is_key_page.is_(True))
        pages = list(self._session.scalars(stmt))

        open_subjects = {
            change.subject
            for change in self._session.scalars(
                select(MonitorChange).where(
                    MonitorChange.client_id == client_id,
                    MonitorChange.scope == "page",
                    MonitorChange.resolved_at.is_(None),
                    MonitorChange.severity.in_(("critical", "warning")),
                )
            )
        }

        def _sort_key(page: MonitorPage) -> tuple[int, int, datetime]:
            priority = 0 if page.is_key_page else (1 if page.url in open_subjects else 2)
            checked = page.last_checked_at or datetime.min.replace(tzinfo=None)
            return (priority, 0, checked)

        return sorted(pages, key=lambda p: _sort_key(p))

    def get_or_create_page(
        self, client_id: str, url: str, *, is_key_page: bool = False
    ) -> MonitorPage:
        page = self.get_page(client_id, url)
        if page is not None:
            if is_key_page and not page.is_key_page:
                page.is_key_page = True
            return page
        page = MonitorPage(client_id=client_id, url=url, is_key_page=is_key_page)
        self._session.add(page)
        self._session.flush()
        return page

    def upsert_page_state(
        self,
        page: MonitorPage,
        *,
        status: int | None,
        final_url: str | None,
        hop_count: int | None,
        indexable: bool | None,
        meta_robots: str | None,
        x_robots_tag: str | None,
        canonical: str | None,
        title: str | None,
        description: str | None,
        h1: str | None,
        word_count: int | None,
        analytics_ids: list[str],
        signals_sha256: str | None,
        text_sha256: str | None,
        raw_sha256: str | None,
        in_sitemap: bool,
        now: datetime | None = None,
    ) -> MonitorPage:
        now = now or utcnow()
        page.status = status
        page.final_url = final_url
        page.hop_count = hop_count
        page.indexable = indexable
        page.meta_robots = meta_robots
        page.x_robots_tag = x_robots_tag
        page.canonical = canonical
        page.title = title
        page.description = description
        page.h1 = h1
        page.word_count = word_count
        page.analytics_ids_json = analytics_ids
        page.signals_sha256 = signals_sha256
        page.text_sha256 = text_sha256
        page.raw_sha256 = raw_sha256
        page.in_sitemap = in_sitemap
        page.last_checked_at = now
        page.consecutive_failures = (
            0 if status and 200 <= status < 400 else page.consecutive_failures + 1
        )
        self._session.flush()
        return page

    def mark_page_gone(self, page: MonitorPage, *, now: datetime | None = None) -> None:
        page.gone_since = page.gone_since or (now or utcnow())

    def clear_page_gone(self, page: MonitorPage) -> None:
        page.gone_since = None

    def create_page_version(
        self,
        page: MonitorPage,
        *,
        http_status: int | None,
        signals: dict[str, Any],
        signals_sha256: str | None,
        text_sha256: str | None,
        raw_sha256: str | None,
        body_blob_sha256: str | None,
        changed_fields: list[str],
        prev_version_id: str | None,
        observed_at: datetime | None = None,
    ) -> MonitorPageVersion:
        version = MonitorPageVersion(
            page_id=page.id,
            client_id=page.client_id,
            observed_at=observed_at or utcnow(),
            http_status=http_status,
            signals_json=signals,
            signals_sha256=signals_sha256,
            text_sha256=text_sha256,
            raw_sha256=raw_sha256,
            body_blob_sha256=body_blob_sha256,
            changed_fields_json=changed_fields,
            prev_version_id=prev_version_id,
        )
        self._session.add(version)
        page.version_seq += 1
        self._session.flush()
        return version

    def latest_page_version(self, page_id: str) -> MonitorPageVersion | None:
        stmt = (
            select(MonitorPageVersion)
            .where(MonitorPageVersion.page_id == page_id)
            .order_by(MonitorPageVersion.observed_at.desc())
            .limit(1)
        )
        return self._session.scalars(stmt).first()

    def page_versions(self, page_id: str, *, limit: int = 50) -> list[MonitorPageVersion]:
        stmt = (
            select(MonitorPageVersion)
            .where(MonitorPageVersion.page_id == page_id)
            .order_by(MonitorPageVersion.observed_at.desc())
            .limit(limit)
        )
        return list(self._session.scalars(stmt))

    def get_page_version(self, version_id: str) -> MonitorPageVersion | None:
        return self._session.get(MonitorPageVersion, version_id)

    def list_pages_page(
        self,
        client_id: str,
        *,
        status: str | None = None,
        indexable: bool | None = None,
        in_sitemap: bool | None = None,
        q: str | None = None,
        sort: str = "last_checked_at",
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[MonitorPage], int]:
        stmt: Select[Any] = select(MonitorPage).where(MonitorPage.client_id == client_id)
        if status == "broken":
            stmt = stmt.where(or_(MonitorPage.status.is_(None), MonitorPage.status >= 300))
        elif status == "ok":
            stmt = stmt.where(and_(MonitorPage.status >= 200, MonitorPage.status < 300))
        if indexable is not None:
            stmt = stmt.where(MonitorPage.indexable == indexable)
        if in_sitemap is not None:
            stmt = stmt.where(MonitorPage.in_sitemap == in_sitemap)
        if q:
            needle = f"%{q.lower()}%"
            stmt = stmt.where(func.lower(MonitorPage.url).like(needle))
        total = self._session.scalar(select(func.count()).select_from(stmt.subquery())) or 0
        descending = sort.startswith("-")
        field = sort[1:] if descending else sort
        sort_column = {
            "last_checked_at": MonitorPage.last_checked_at,
            "status": MonitorPage.status,
            "url": MonitorPage.url,
            "version_seq": MonitorPage.version_seq,
        }.get(field, MonitorPage.last_checked_at)
        order = sort_column.desc() if descending else sort_column.asc()
        stmt = stmt.order_by(order.nulls_last()).limit(limit).offset(offset)
        return list(self._session.scalars(stmt)), total

    def sitemap_health_counts(self, client_id: str) -> dict[str, int]:
        """`{"total", "ok", "redirects", "broken", "unavailable"}` over every
        URL currently in the client's sitemap (task-35 §3.4.1) — always fresh,
        computed straight from `MonitorPage` state rather than cached.
        """
        from app.monitor.severity import classify_sitemap_status

        stmt = select(MonitorPage.status).where(
            MonitorPage.client_id == client_id, MonitorPage.in_sitemap.is_(True)
        )
        counts = {"total": 0, "ok": 0, "redirects": 0, "broken": 0, "unavailable": 0}
        for (status,) in self._session.execute(stmt):
            counts["total"] += 1
            kind = classify_sitemap_status(status)
            counts["redirects" if kind == "redirect" else kind] += 1
        return counts

    # --- DNS -----------------------------------------------------------

    def get_dns_record(
        self, client_id: str, name: str, record_type: str
    ) -> MonitorDnsRecord | None:
        stmt = select(MonitorDnsRecord).where(
            MonitorDnsRecord.client_id == client_id,
            MonitorDnsRecord.name == name,
            MonitorDnsRecord.record_type == record_type,
        )
        return self._session.scalars(stmt).first()

    def all_dns_records(self, client_id: str) -> list[MonitorDnsRecord]:
        stmt = select(MonitorDnsRecord).where(MonitorDnsRecord.client_id == client_id)
        return list(
            self._session.scalars(
                stmt.order_by(MonitorDnsRecord.name, MonitorDnsRecord.record_type)
            )
        )

    def upsert_dns_state(
        self,
        client_id: str,
        name: str,
        record_type: str,
        *,
        values: list[str],
        ttl: int | None,
        resolver: str | None,
        now: datetime | None = None,
    ) -> MonitorDnsRecord:
        now = now or utcnow()
        record = self.get_dns_record(client_id, name, record_type)
        if record is None:
            record = MonitorDnsRecord(client_id=client_id, name=name, record_type=record_type)
            self._session.add(record)
        record.values_json = values
        record.ttl = ttl
        record.resolver = resolver
        record.last_checked_at = now
        record.updated_at = now
        self._session.flush()
        return record

    def create_dns_version(
        self,
        record: MonitorDnsRecord,
        *,
        values: list[str],
        ttl: int | None,
        resolver: str | None,
        prev_version_id: str | None,
        observed_at: datetime | None = None,
    ) -> MonitorDnsVersion:
        version = MonitorDnsVersion(
            record_id=record.id,
            client_id=record.client_id,
            observed_at=observed_at or utcnow(),
            values_json=values,
            ttl=ttl,
            resolver=resolver,
            prev_version_id=prev_version_id,
        )
        self._session.add(version)
        record.version_seq += 1
        self._session.flush()
        return version

    def latest_dns_version(self, record_id: str) -> MonitorDnsVersion | None:
        stmt = (
            select(MonitorDnsVersion)
            .where(MonitorDnsVersion.record_id == record_id)
            .order_by(MonitorDnsVersion.observed_at.desc())
            .limit(1)
        )
        return self._session.scalars(stmt).first()

    def dns_versions(self, record_id: str, *, limit: int = 50) -> list[MonitorDnsVersion]:
        stmt = (
            select(MonitorDnsVersion)
            .where(MonitorDnsVersion.record_id == record_id)
            .order_by(MonitorDnsVersion.observed_at.desc())
            .limit(limit)
        )
        return list(self._session.scalars(stmt))

    # --- changes (the alert feed) ----------------------------------------

    def open_change(self, client_id: str, change_type: str, subject: str) -> MonitorChange | None:
        stmt = select(MonitorChange).where(
            MonitorChange.client_id == client_id,
            MonitorChange.change_type == change_type,
            MonitorChange.subject == subject,
            MonitorChange.resolved_at.is_(None),
        )
        return self._session.scalars(stmt).first()

    def record_change(
        self,
        client_id: str,
        *,
        run_id: str | None,
        severity: str,
        change_type: str,
        scope: str,
        subject: str,
        related_id: str | None = None,
        old_value: str | None = None,
        new_value: str | None = None,
        details: dict[str, Any] | None = None,
        now: datetime | None = None,
    ) -> MonitorChange:
        change = MonitorChange(
            client_id=client_id,
            run_id=run_id,
            severity=severity,
            change_type=change_type,
            scope=scope,
            subject=subject,
            related_id=related_id,
            old_value=old_value,
            new_value=new_value,
            details_json=details or {},
            detected_at=now or utcnow(),
        )
        self._session.add(change)
        self._session.flush()
        return change

    def resolve_change(self, change: MonitorChange, *, now: datetime | None = None) -> None:
        change.resolved_at = now or utcnow()

    def ack_change(self, change_id: str, *, note: str | None) -> MonitorChange | None:
        change = self._session.get(MonitorChange, change_id)
        if change is None:
            return None
        change.acknowledged_at = utcnow()
        change.acknowledged_note = note
        self._session.flush()
        return change

    def get_change(self, change_id: str) -> MonitorChange | None:
        return self._session.get(MonitorChange, change_id)

    def list_changes(
        self,
        client_id: str | None = None,
        *,
        severity: str | None = None,
        change_type: str | None = None,
        state: ChangeState = "open",
        limit: int = 100,
    ) -> list[MonitorChange]:
        stmt = select(MonitorChange)
        if client_id is not None:
            stmt = stmt.where(MonitorChange.client_id == client_id)
        if severity is not None:
            stmt = stmt.where(MonitorChange.severity == severity)
        if change_type is not None:
            stmt = stmt.where(MonitorChange.change_type == change_type)
        if state == "open":
            stmt = stmt.where(MonitorChange.resolved_at.is_(None))
        stmt = stmt.order_by(MonitorChange.detected_at.desc()).limit(limit)
        return list(self._session.scalars(stmt))

    def open_counts_by_severity(self, client_id: str) -> dict[str, int]:
        stmt = (
            select(MonitorChange.severity, func.count())
            .where(MonitorChange.client_id == client_id, MonitorChange.resolved_at.is_(None))
            .group_by(MonitorChange.severity)
        )
        counts = {"critical": 0, "warning": 0, "info": 0}
        for severity, count in self._session.execute(stmt):
            counts[severity] = count
        return counts

    def count_changes_by_severity(self, run_id: str) -> dict[str, int]:
        """What *this run* found, not what is currently open client-wide —
        distinct from `open_counts_by_severity` (task-37 F4)."""
        stmt = (
            select(MonitorChange.severity, func.count())
            .where(MonitorChange.run_id == run_id)
            .group_by(MonitorChange.severity)
        )
        counts = {"critical": 0, "warning": 0, "info": 0}
        for severity, count in self._session.execute(stmt):
            counts[severity] = count
        return counts

    def portfolio_open_severity_counts(
        self, client_ids: Sequence[str]
    ) -> dict[str, dict[str, int]]:
        if not client_ids:
            return {}
        stmt = (
            select(MonitorChange.client_id, MonitorChange.severity, func.count())
            .where(MonitorChange.client_id.in_(client_ids), MonitorChange.resolved_at.is_(None))
            .group_by(MonitorChange.client_id, MonitorChange.severity)
        )
        result: dict[str, dict[str, int]] = {
            cid: {"critical": 0, "warning": 0, "info": 0} for cid in client_ids
        }
        for client_id, severity, count in self._session.execute(stmt):
            result.setdefault(client_id, {"critical": 0, "warning": 0, "info": 0})[severity] = count
        return result
