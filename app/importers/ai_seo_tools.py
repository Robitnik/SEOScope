"""AISeoTools history importer orchestration (task-07): scan -> map clients
-> parse each recognized artifact -> write (or just report, in `--dry-run`).

One artifact = one short DB transaction (mirrors
`app.db.session.worker_session_scope`) so one bad file can never abort the
whole import — the same cell-isolation spirit CLAUDE.md asks of live
collection (PLAN §9.2: a failure in one `(client, provider)` cell must never
take down the others).
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from sqlalchemy.orm import Session, sessionmaker

from app.db.models import Client
from app.db.repositories.clients import ClientRepository
from app.db.repositories.imports import ImportRepository
from app.db.repositories.jobs import JobRepository
from app.db.repositories.metrics import MetricRepository
from app.db.repositories.snapshots import SnapshotRepository
from app.importers.client_mapping import (
    ClientEntry,
    ClientMap,
    load_client_map,
    normalize_code,
    normalize_domain,
)
from app.importers.parsers import ParsedArtifact, ParseSkip
from app.importers.parsers import ahrefs as ahrefs_parser
from app.importers.parsers import client_report as client_report_parser
from app.importers.parsers import gsc as gsc_parser
from app.importers.parsers import rank_movement as rank_movement_parser
from app.importers.parsers import semrush as semrush_parser
from app.importers.scanner import RunArtifact, scan_source
from app.metrics.normalize import normalize_result
from app.schemas.adapter import CollectionResult
from app.schemas.enums import Provider

ParseFn = Callable[..., "list[ParsedArtifact] | ParseSkip"]

_PARSERS: dict[str, tuple[ParseFn, str]] = {
    "ahrefs": (ahrefs_parser.parse, ahrefs_parser.IMPORTER_VERSION),
    "gsc": (gsc_parser.parse, gsc_parser.IMPORTER_VERSION),
    "semrush": (semrush_parser.parse, semrush_parser.IMPORTER_VERSION),
    "rank_movement": (rank_movement_parser.parse, rank_movement_parser.IMPORTER_VERSION),
    "client_report": (client_report_parser.parse, client_report_parser.IMPORTER_VERSION),
}

ArtifactOutcome = Literal[
    "imported",
    "skipped_recognized",
    "skipped_conflict",
    "skipped_unmappable",
    "skipped_already_seen",
    "skipped_not_selected",
]


@dataclass(frozen=True)
class ArtifactReport:
    relative_path: str
    kind: str
    domain_or_code: str
    outcome: ArtifactOutcome
    detail: str
    metrics_count: int = 0


@dataclass
class ImportReport:
    conflicts: dict[str, list[str]] = field(default_factory=dict)
    unmappable_codes: list[str] = field(default_factory=list)
    artifacts: list[ArtifactReport] = field(default_factory=list)

    @property
    def imported_count(self) -> int:
        return sum(1 for a in self.artifacts if a.outcome == "imported")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "client"


def _resolve_domain(artifact: RunArtifact, client_map: ClientMap) -> tuple[str | None, str | None]:
    """`(domain, matched_code)`; `(None, None)` if nothing on disk lets this
    artifact be traced back to a domain at all.
    """
    if artifact.kind == "client_report":
        target = normalize_code(artifact.domain_or_code)
        for code, entry in client_map.entries.items():
            if normalize_code(code) == target:
                return entry.domain, code
        return None, None
    domain = normalize_domain(artifact.domain_or_code)
    if domain is None:
        return None, None
    codes = client_map.domain_to_codes.get(domain)
    matched_code = codes[0] if codes and len(codes) == 1 else None
    return domain, matched_code


def _sync_integrations(client_repo: ClientRepository, client_id: str, entry: ClientEntry) -> None:
    def merged(provider: Provider, values: Mapping[str, str | None]) -> dict[str, str]:
        existing = client_repo.get_integration(client_id, provider)
        config = dict(existing.config_json or {}) if existing is not None else {}
        config.update({key: value for key, value in values.items() if value})
        return config

    if entry.gsc_account_id:
        client_repo.upsert_integration(
            client_id,
            Provider.gsc,
            config=merged(
                Provider.gsc,
                {"account_id": entry.gsc_account_id, "connection_key": entry.connection_key},
            ),
        )
    if entry.ga4_account_id:
        config: dict[str, str] = {
            "account_id": entry.ga4_account_id,
            "connection_key": entry.connection_key or "",
        }
        if entry.ninja_report_name:
            config["ninja_report_name"] = entry.ninja_report_name
        client_repo.upsert_integration(
            client_id,
            Provider.reporting_ninja,
            config=merged(Provider.reporting_ninja, config),
        )


def _get_or_create_client(
    client_repo: ClientRepository, domain: str, matched_code: str | None, client_map: ClientMap
) -> Client:
    existing = client_repo.get_by_domain(domain)
    if existing is not None:
        if matched_code is not None:
            _sync_integrations(client_repo, existing.id, client_map.entries[matched_code])
        return existing
    # Prefer the human-authored clients.json code as the display name; a
    # bare domain is the only honest fallback when there's no such code —
    # never invent a nicer-looking name from the domain string.
    name = matched_code if matched_code is not None else domain
    client = client_repo.create(name=name, slug=_slugify(name), domain=domain)
    if matched_code is not None:
        _sync_integrations(client_repo, client.id, client_map.entries[matched_code])
    return client


def _sync_existing_client_integrations(
    client_map: ClientMap,
    session_factory: sessionmaker[Session],
    allowed_domains: set[str] | None,
) -> None:
    """Apply ``clients.json`` mappings even when every artifact was seen before.

    Integration sync used to happen only while creating a brand-new client.
    In this application the roster is seeded first, so a perfectly valid
    repeat import never connected its GSC/GA4 references.
    """
    session = session_factory()
    try:
        clients = ClientRepository(session)
        for entry in client_map.entries.values():
            if entry.domain is None:
                continue
            if allowed_domains is not None and entry.domain not in allowed_domains:
                continue
            client = clients.get_by_domain(entry.domain)
            if client is not None:
                _sync_integrations(clients, client.id, entry)
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def run_import(
    *,
    source: Path,
    dry_run: bool,
    session_factory: sessionmaker[Session],
    allowed_domains: set[str] | None = None,
) -> ImportReport:
    client_map = load_client_map(source)
    report = ImportReport(
        conflicts=client_map.conflicts, unmappable_codes=client_map.unmappable_codes
    )

    normalized_allowed = (
        {domain for raw in allowed_domains if (domain := normalize_domain(raw)) is not None}
        if allowed_domains is not None
        else None
    )
    if not dry_run:
        _sync_existing_client_integrations(client_map, session_factory, normalized_allowed)

    artifacts: list[RunArtifact] = sorted(scan_source(source), key=lambda a: a.relative_path)

    for artifact in artifacts:
        domain, matched_code = _resolve_domain(artifact, client_map)

        if (
            domain is not None
            and normalized_allowed is not None
            and domain not in normalized_allowed
        ):
            report.artifacts.append(
                ArtifactReport(
                    relative_path=artifact.relative_path,
                    kind=artifact.kind,
                    domain_or_code=domain,
                    outcome="skipped_not_selected",
                    detail="domain is outside the selected client roster",
                )
            )
            continue

        if domain is not None and domain in client_map.conflicts:
            report.artifacts.append(
                ArtifactReport(
                    relative_path=artifact.relative_path,
                    kind=artifact.kind,
                    domain_or_code=artifact.domain_or_code,
                    outcome="skipped_conflict",
                    detail=(
                        f"domain {domain!r} claimed by multiple clients.json codes: "
                        f"{client_map.conflicts[domain]}"
                    ),
                )
            )
            continue
        if domain is None:
            report.artifacts.append(
                ArtifactReport(
                    relative_path=artifact.relative_path,
                    kind=artifact.kind,
                    domain_or_code=artifact.domain_or_code,
                    outcome="skipped_unmappable",
                    detail="no derivable domain for this artifact",
                )
            )
            continue

        primary_path = next(iter(artifact.files.values()))
        sha256 = _sha256_file(primary_path)
        parse_fn, importer_version = _PARSERS[artifact.kind]

        session = session_factory()
        try:
            import_repo = ImportRepository(session)
            if import_repo.seen(sha256):
                report.artifacts.append(
                    ArtifactReport(
                        relative_path=artifact.relative_path,
                        kind=artifact.kind,
                        domain_or_code=domain,
                        outcome="skipped_already_seen",
                        detail="checksum already in imported_sources",
                    )
                )
                session.rollback()
                continue

            parsed = parse_fn(artifact.files, fallback_date=artifact.run_date)

            if isinstance(parsed, ParseSkip):
                if not dry_run:
                    import_repo.record(artifact.relative_path, sha256, importer_version)
                    session.commit()
                else:
                    session.rollback()
                report.artifacts.append(
                    ArtifactReport(
                        relative_path=artifact.relative_path,
                        kind=artifact.kind,
                        domain_or_code=domain,
                        outcome="skipped_recognized",
                        detail=parsed.reason,
                    )
                )
                continue

            metrics_count = sum(len(piece.metrics) for piece in parsed)
            if not dry_run:
                client_repo = ClientRepository(session)
                client = _get_or_create_client(client_repo, domain, matched_code, client_map)
                job_repo = JobRepository(session)
                snapshot_repo = SnapshotRepository(session)
                metric_repo = MetricRepository(session)

                written = 0
                for piece in parsed:
                    job, _cell = job_repo.create_imported_run(
                        client_id=client.id, provider=piece.provider, observed_at=piece.observed_at
                    )
                    snapshot = snapshot_repo.create_immutable(
                        job_id=job.id,
                        client_id=client.id,
                        provider=piece.provider,
                        source_mode=piece.source_mode,
                        observed_at=piece.observed_at,
                        quality=piece.quality,
                        source_version=importer_version,
                        data_through=piece.data_through,
                        warnings=piece.warnings,
                        provenance=piece.provenance,
                    )
                    normalized = normalize_result(
                        CollectionResult(
                            provider=piece.provider,
                            quality=piece.quality,
                            observed_at=piece.observed_at,
                            data_through=piece.data_through,
                            metrics=piece.metrics,
                            warnings=piece.warnings,
                            provenance=piece.provenance,
                        )
                    )
                    rows = metric_repo.bulk_insert(
                        snapshot_id=snapshot.id,
                        client_id=client.id,
                        provider=piece.provider,
                        values=normalized,
                    )
                    written += len(rows)

                import_repo.record(artifact.relative_path, sha256, importer_version)
                session.commit()
                metrics_count = written
            else:
                session.rollback()

            report.artifacts.append(
                ArtifactReport(
                    relative_path=artifact.relative_path,
                    kind=artifact.kind,
                    domain_or_code=domain,
                    outcome="imported",
                    detail=f"{len(parsed)} snapshot(s)" if not dry_run else "would import",
                    metrics_count=metrics_count,
                )
            )
        except Exception as exc:  # one bad artifact must never abort the whole run
            session.rollback()
            report.artifacts.append(
                ArtifactReport(
                    relative_path=artifact.relative_path,
                    kind=artifact.kind,
                    domain_or_code=domain,
                    outcome="skipped_recognized",
                    detail=f"unexpected error: {exc}",
                )
            )
        finally:
            session.close()

    return report


def render_report(report: ImportReport, *, dry_run: bool) -> str:
    lines: list[str] = []
    mode = "DRY RUN" if dry_run else "IMPORT"
    lines.append(f"=== {mode}: client mapping ===")
    if report.conflicts:
        lines.append("CONFLICTS (blocked until resolved in clients.json):")
        for domain, codes in sorted(report.conflicts.items()):
            lines.append(f"  - {domain}: claimed by {codes}")
    else:
        lines.append("  no mapping conflicts")
    if report.unmappable_codes:
        lines.append("UNMAPPABLE (configured but no derivable domain):")
        for code in sorted(report.unmappable_codes):
            lines.append(f"  - {code}")

    lines.append("")
    lines.append("=== artifacts ===")
    for a in sorted(report.artifacts, key=lambda r: r.relative_path):
        lines.append(f"[{a.outcome}] {a.relative_path} ({a.kind}, {a.domain_or_code}): {a.detail}")

    counts: dict[str, int] = {}
    for a in report.artifacts:
        counts[a.outcome] = counts.get(a.outcome, 0) + 1
    lines.append("")
    lines.append("=== summary ===")
    for outcome, count in sorted(counts.items()):
        lines.append(f"  {outcome}: {count}")
    lines.append(f"  total artifacts scanned: {len(report.artifacts)}")
    return "\n".join(lines)
