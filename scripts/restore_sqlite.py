#!/usr/bin/env python
"""Відновлення SQLite + artifacts із бекапу (PLAN §18).

Порядок дій навмисно жорсткий, бо відновлення — незворотна операція:

1. `docker compose stop api worker` — **обов'язково**; писати в БД під час
   відновлення не можна, а один writer гарантує тільки зупинений worker;
2. цей скрипт перевіряє контрольну суму й `integrity_check` бекапу **до**
   того, як торкнеться робочої бази;
3. поточна база відкладається вбік (`.superseded-<стамп>`), а не стирається;
4. `docker compose up -d` — entrypoint сам прожене `alembic upgrade head`.

    docker compose run --rm api poetry run python scripts/restore_sqlite.py \
        --from /app/app_data/backups/app-20260722T101500Z.db --force
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


def verify_backup(backup: Path) -> None:
    if not backup.is_file():
        raise SystemExit(f"бекапу немає: {backup}")

    sidecar = backup.with_suffix(backup.suffix + ".sha256")
    if sidecar.is_file():
        expected = sidecar.read_text().split()[0]
        actual = _sha256(backup)
        if expected != actual:
            raise SystemExit(f"контрольна сума не збіглась: очікували {expected}, маємо {actual}")
    else:
        print(f"увага: немає {sidecar.name}, перевірка контрольної суми пропущена")

    connection = sqlite3.connect(f"file:{backup}?mode=ro", uri=True)
    try:
        result = connection.execute("PRAGMA integrity_check").fetchone()[0]
        tables = [row[0] for row in connection.execute("SELECT name FROM sqlite_master")]
    except sqlite3.DatabaseError as exc:
        # Не трейсбек: оператор має побачити зрозумілу відмову ще до того,
        # як щось торкнеться робочої бази.
        raise SystemExit(f"файл не читається як SQLite ({exc}): {backup}") from exc
    finally:
        connection.close()
    if result != "ok":
        raise SystemExit(f"бекап пошкоджений: {result}")
    if "clients" not in tables:
        raise SystemExit("у бекапі немає таблиці `clients` — це не база цього застосунку")


def move_aside(target: Path, stamp: str) -> list[Path]:
    """Відкласти поточну БД разом із WAL/SHM — стирати нічого не можна."""
    moved: list[Path] = []
    for suffix in ("", "-wal", "-shm"):
        current = Path(str(target) + suffix)
        if current.exists():
            superseded = Path(f"{current}.superseded-{stamp}")
            current.rename(superseded)
            moved.append(superseded)
    return moved


def restore_artifacts(archive: Path, artifacts_dir: Path, stamp: str) -> None:
    if not archive.is_file():
        raise SystemExit(f"архіву artifacts немає: {archive}")
    if artifacts_dir.exists():
        artifacts_dir.rename(Path(f"{artifacts_dir}.superseded-{stamp}"))
    artifacts_dir.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "r:gz") as tar:
        # `filter="data"` (Python 3.12+) відкидає абсолютні шляхи й `..`
        # усередині архіву — навіть власний бекап розпаковується не наосліп.
        tar.extractall(artifacts_dir.parent, filter="data")


def main(argv: list[str] | None = None) -> int:
    from app.core.config import get_settings

    settings = get_settings()
    parser = argparse.ArgumentParser(prog="restore_sqlite")
    parser.add_argument("--from", dest="backup", required=True, type=Path)
    parser.add_argument("--artifacts-archive", type=Path, default=None)
    parser.add_argument("--database-url", default=settings.database_url)
    parser.add_argument("--artifacts-dir", type=Path, default=settings.artifacts_dir)
    parser.add_argument(
        "--force",
        action="store_true",
        help="підтвердити перезапис робочої бази (без прапорця скрипт нічого не робить)",
    )
    args = parser.parse_args(argv)

    target = _sqlite_path(args.database_url)
    verify_backup(args.backup)
    print(f"бекап перевірено: {args.backup}")

    if not args.force:
        print(
            f"це замінить {target}. Зупини api й worker "
            "(`docker compose stop api worker`) і повтори з --force."
        )
        return 1

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    for moved in move_aside(target, stamp):
        print(f"відкладено -> {moved}")

    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(args.backup, target)
    print(f"відновлено -> {target}")

    if args.artifacts_archive is not None:
        restore_artifacts(args.artifacts_archive, args.artifacts_dir, stamp)
        print(f"artifacts  -> {args.artifacts_dir}")

    print("готово. Далі: docker compose up -d (entrypoint прожене alembic upgrade head)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
