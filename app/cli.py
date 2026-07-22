"""CLI entrypoint (task-07): `poetry run yovth-report <command>`.

Registered in `pyproject.toml` as `[tool.poetry.scripts] yovth-report =
"app.cli:main"`.
"""

from __future__ import annotations

import argparse
import sys
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

    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
