"""CLI entrypoint (task-07): `poetry run yovth-report <command>`.

Registered in `pyproject.toml` as `[tool.poetry.scripts] yovth-report =
"app.cli:main"`.
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.db.session import get_sync_sessionmaker
from app.importers.ai_seo_tools import render_report, run_import


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="yovth-report")
    subparsers = parser.add_subparsers(dest="command", required=True)

    import_cmd = subparsers.add_parser(
        "import-ai-seo-history",
        help=(
            "Read-only, idempotent backfill of provider_snapshots/metric_values "
            "from AISeoTools history (PLAN §11)"
        ),
    )
    import_cmd.add_argument(
        "--source",
        required=True,
        type=Path,
        help="Path to the AISeoTools checkout root (contains clients.json and out/)",
    )
    import_cmd.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be imported without writing to the database",
    )

    backfill_cmd = subparsers.add_parser(
        "backfill-health-history",
        help=(
            "Rebuild the daily composite-health series from snapshots already "
            "stored, so the dashboard card's trend works without waiting for "
            "new history to accumulate (idempotent)"
        ),
    )
    backfill_cmd.add_argument(
        "--days",
        type=int,
        default=30,
        help="How many days back to reconstruct (default: 30)",
    )

    daily_backfill_cmd = subparsers.add_parser(
        "backfill-daily-history",
        help=(
            "Queue a custom-period sync so day-level GSC/GA4 history exists for "
            "the Portfolio Trends page (`#/trends`, task-28). METERED: this "
            "creates real provider requests through the normal job engine, not "
            "a local recomputation."
        ),
    )
    daily_backfill_cmd.add_argument(
        "--days", type=int, default=90, help="How many days back to collect (default: 90)"
    )
    daily_backfill_cmd.add_argument(
        "--providers",
        default="gsc,reporting_ninja",
        help="Comma-separated providers to collect (default: gsc,reporting_ninja)",
    )
    daily_backfill_cmd.add_argument(
        "--client-ids",
        default=None,
        help="Comma-separated client IDs (default: every enabled client)",
    )
    daily_backfill_cmd.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the plan (range, clients, estimated requests) without creating a job",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "import-ai-seo-history":
        source: Path = args.source
        if not source.is_dir():
            print(f"error: --source is not a directory: {source}", file=sys.stderr)
            return 2

        report = run_import(
            source=source, dry_run=args.dry_run, session_factory=get_sync_sessionmaker()
        )
        print(render_report(report, dry_run=args.dry_run))
        return 0

    if args.command == "backfill-health-history":
        # Imported here, not at module scope: this pulls in the API layer,
        # which the import command has no reason to load.
        from app.api.dashboard import backfill_health_history

        if args.days < 1:
            print("error: --days must be >= 1", file=sys.stderr)
            return 2

        session_factory = get_sync_sessionmaker()
        with session_factory() as session:
            written = backfill_health_history(
                session, days=args.days, now=datetime.now(UTC)
            )
            session.commit()
        print(f"recorded {written} daily health point(s) across the last {args.days} day(s)")
        return 0

    if args.command == "backfill-daily-history":
        # Imported here, not at module scope: same rationale as the health
        # backfill above — this pulls in the DB/job-engine layer, which the
        # import command has no reason to load.
        from app.db.repositories.clients import ClientRepository
        from app.db.repositories.jobs import JobRepository
        from app.schemas.enums import ComparisonMode, PeriodKind, Provider

        if args.days < 1:
            print("error: --days must be >= 1", file=sys.stderr)
            return 2

        provider_names = [p.strip() for p in args.providers.split(",") if p.strip()]
        if not provider_names:
            print("error: --providers must name at least one provider", file=sys.stderr)
            return 2
        try:
            providers = [Provider(name) for name in provider_names]
        except ValueError as exc:
            print(f"error: unknown provider in --providers: {exc}", file=sys.stderr)
            return 2

        session_factory = get_sync_sessionmaker()
        with session_factory() as session:
            clients_repo = ClientRepository(session)
            if args.client_ids:
                requested = [c.strip() for c in args.client_ids.split(",") if c.strip()]
                found = [clients_repo.get(cid) for cid in requested]
                pairs = zip(requested, found, strict=True)
                missing = [cid for cid, client in pairs if client is None]
                if missing:
                    print(
                        f"error: unknown --client-ids: {', '.join(missing)}", file=sys.stderr
                    )
                    return 2
                clients = [client for client in found if client is not None]
            else:
                clients = clients_repo.list_active()

            if not clients:
                print("error: no clients to collect (empty client list)", file=sys.stderr)
                return 2

            window_end = datetime.now(UTC).date() - timedelta(days=1)
            window_start = window_end - timedelta(days=args.days - 1)
            cell_count = len(clients) * len(providers)

            print(f"range: {window_start} .. {window_end} ({args.days} day(s))")
            print(
                f"clients: {len(clients)}  providers: {', '.join(p.value for p in providers)}"
            )
            print(f"cells to queue: {cell_count}")
            print(
                "note: each cell issues multiple provider requests (GSC ~2 queries, "
                "GA4 ~12 queries per client) — this is a metered live collection, "
                "not a local computation. --dry-run does not spend units."
            )
            if args.dry_run:
                print("dry run: no job created")
                return 0

            # `force_refresh`: without it, a cell whose provider already has a
            # recent snapshot (the normal weekly sync keeps one) is skipped by
            # the freshness gate regardless of which period was requested —
            # silently producing zero new daily rows despite "queued" below.
            jobs = JobRepository(session).create_run(
                client_ids=[client.id for client in clients],
                providers=providers,
                period_kind=PeriodKind.custom,
                comparison_mode=ComparisonMode.closed,
                anchor_date=window_end,
                custom_start=window_start,
                custom_end=window_end,
                mode="force_refresh",
            )
            session.commit()
        print(f"queued {len(jobs)} job(s); the running worker will collect them")
        return 0

    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
