#!/usr/bin/env python
"""Консистентний бекап SQLite + artifacts (PLAN §18, Етап 8).

Використовує **онлайн-бекап API** SQLite, а не копіювання файлу: база
працює в WAL і в момент бекапу її пише worker, тож `cp app.db` дав би
пошкоджений знімок без `-wal`/`-shm`. `Connection.backup()` натомість
віддає узгоджений стан однією транзакцією і не зупиняє застосунок.

Запускати завжди в контейнері (CLAUDE.md «Середовище виконання»):

    docker compose exec api poetry run python scripts/backup_sqlite.py \
        --out-dir /app/app_data/backups --keep 14
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import sqlite3
import sys
import tarfile
from datetime import UTC, datetime
from pathlib import Path


def _sqlite_path(database_url: str) -> Path:
    """`sqlite+aiosqlite:///./app_data/app.db` -> `Path("./app_data/app.db")`."""
    for prefix in ("sqlite+aiosqlite:///", "sqlite:///"):
        if database_url.startswith(prefix):
            return Path(database_url[len(prefix) :])
    raise SystemExit(f"підтримується лише SQLite URL, отримано: {database_url!r}")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def backup_database(source: Path, target: Path) -> None:
    if not source.is_file():
        raise SystemExit(f"файлу БД немає: {source}")
    target.parent.mkdir(parents=True, exist_ok=True)

    source_connection = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    target_connection = sqlite3.connect(target)
    try:
        source_connection.backup(target_connection)
    finally:
        target_connection.close()
        source_connection.close()

    verify = sqlite3.connect(f"file:{target}?mode=ro", uri=True)
    try:
        result = verify.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        verify.close()
    if result != "ok":
        target.unlink(missing_ok=True)
        raise SystemExit(f"бекап не пройшов integrity_check: {result}")


def archive_artifacts(artifacts_dir: Path, target: Path) -> Path | None:
    if not artifacts_dir.is_dir():
        return None
    target.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(target, "w:gz") as archive:
        archive.add(artifacts_dir, arcname=artifacts_dir.name)
    return target


def rotate(out_dir: Path, keep: int) -> list[Path]:
    """Лишити `keep` найновіших бекапів, решту прибрати разом із супутниками."""
    if keep <= 0:
        return []
    snapshots = sorted(out_dir.glob("app-*.db"), reverse=True)
    removed: list[Path] = []
    for stale in snapshots[keep:]:
        stamp = stale.stem[len("app-") :]
        companions = [
            stale,
            out_dir / f"{stale.name}.sha256",
            out_dir / f"artifacts-{stamp}.tar.gz",
        ]
        for companion in companions:
            if companion.exists():
                companion.unlink()
                removed.append(companion)
    return removed


def main(argv: list[str] | None = None) -> int:
    from app.core.config import get_settings

    settings = get_settings()
    parser = argparse.ArgumentParser(prog="backup_sqlite")
    parser.add_argument("--database-url", default=settings.database_url)
    parser.add_argument("--artifacts-dir", type=Path, default=settings.artifacts_dir)
    parser.add_argument("--out-dir", type=Path, default=Path("./app_data/backups"))
    parser.add_argument(
        "--keep", type=int, default=14, help="скільки останніх бекапів лишати (0 = не ротувати)"
    )
    parser.add_argument(
        "--skip-artifacts", action="store_true", help="бекапити лише БД, без artifacts"
    )
    args = parser.parse_args(argv)

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out_dir: Path = args.out_dir
    db_target = out_dir / f"app-{stamp}.db"

    backup_database(_sqlite_path(args.database_url), db_target)
    checksum = _sha256(db_target)
    (out_dir / f"app-{stamp}.db.sha256").write_text(f"{checksum}  {db_target.name}\n")
    print(f"БД      -> {db_target} ({db_target.stat().st_size} байт, sha256 {checksum[:12]}…)")

    if not args.skip_artifacts:
        archive = archive_artifacts(args.artifacts_dir, out_dir / f"artifacts-{stamp}.tar.gz")
        if archive is None:
            print(f"artifacts -> пропущено, директорії немає: {args.artifacts_dir}")
        else:
            print(f"artifacts -> {archive} ({archive.stat().st_size} байт)")

    for removed in rotate(out_dir, args.keep):
        print(f"ротація -> прибрано {removed.name}")

    free = shutil.disk_usage(out_dir).free
    print(f"готово; вільно на диску: {free // (1024 * 1024)} МіБ")
    return 0


if __name__ == "__main__":
    sys.exit(main())
