#!/usr/bin/env python3
"""DB throughput bench harness (task-66 §7/§9) — reproduces the exact
probes §0 of the task plan used to diagnose the 2026-09-05 lock storm, so
"before" and "after" phase A/B/D can be compared on the same numbers
instead of vibes.

Run against a COPY of the production database, never the live file — see
`scripts/backup_sqlite.py`. Example:

    docker compose exec api poetry run python scripts/db_bench.py \\
        --db /tmp/bench.db --seconds 20

Every probe here is read-only with respect to application data (it uses
its own scratch table, `_db_bench_probe`, dropped at the end) except the
commit-cost probe, which is exactly what it measures.
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import statistics
import sys
import threading
import time
from pathlib import Path


def probe_lock_occupancy(db_path: Path, seconds: float, interval: float = 0.1) -> None:
    """§0.3/§0.4: sample how much of the window the writer lock is BUSY —
    the single most important number this task cares about. `busy_timeout=0`
    so a failed attempt returns immediately instead of waiting.
    """
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA busy_timeout=0")
    busy = 0
    free = 0
    acquire_times_ms: list[float] = []
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        start = time.perf_counter()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.rollback()
            free += 1
            acquire_times_ms.append((time.perf_counter() - start) * 1000)
        except sqlite3.OperationalError:
            busy += 1
        time.sleep(interval)
    conn.close()
    total = busy + free
    print(f"\n== writer lock occupancy ({seconds:.0f}s sample, {interval * 1000:.0f}ms step) ==")
    if total == 0:
        print("  no samples taken")
        return
    print(f"  BUSY: {busy}/{total} ({busy / total * 100:.0f}%)")
    print(f"  FREE: {free}/{total} ({free / total * 100:.0f}%)")
    if acquire_times_ms:
        print(
            f"  acquire time when free: p50={statistics.median(acquire_times_ms):.2f}ms "
            f"max={max(acquire_times_ms):.2f}ms"
        )


def probe_commit_cost(db_path: Path, synchronous: str, n: int = 200) -> float:
    """§0.2: cost of one single-row commit at a given `synchronous` level,
    on a scratch table so this never touches real application rows.
    """
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(f"PRAGMA synchronous={synchronous}")
    conn.execute("CREATE TABLE IF NOT EXISTS _db_bench_probe(x INTEGER)")
    conn.commit()
    start = time.perf_counter()
    for i in range(n):
        conn.execute("INSERT INTO _db_bench_probe VALUES (?)", (i,))
        conn.commit()
    elapsed = time.perf_counter() - start
    conn.execute("DROP TABLE _db_bench_probe")
    conn.commit()
    conn.close()
    per_commit_ms = elapsed / n * 1000
    print(
        f"  synchronous={synchronous:<8} {n} commits in {elapsed * 1000:.0f}ms "
        f"-> {per_commit_ms:.2f}ms/commit"
    )
    return per_commit_ms


def probe_pool_concurrency(db_path: Path, concurrencies: list[int], hold_ms: float = 50.0) -> None:
    """§0.4: read latency under N concurrent short-lived connections, each
    holding its connection open for `hold_ms` to simulate real request
    work — the same shape that showed the old 15-connection pool bottleneck
    in waves of exactly its own size.
    """
    def _one(lock: threading.Lock, latencies_ms: list[float]) -> None:
        start = time.perf_counter()
        conn = sqlite3.connect(db_path)
        try:
            conn.execute("SELECT 1").fetchone()
            time.sleep(hold_ms / 1000)
        finally:
            conn.close()
        with lock:
            latencies_ms.append((time.perf_counter() - start) * 1000)

    print(f"\n== concurrent connection latency (each holds {hold_ms:.0f}ms) ==")
    for n in concurrencies:
        latencies_ms: list[float] = []
        lock = threading.Lock()

        threads = [threading.Thread(target=_one, args=(lock, latencies_ms)) for _ in range(n)]
        wall_start = time.perf_counter()
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        wall_ms = (time.perf_counter() - wall_start) * 1000
        print(
            f"  concurrency={n:>3}  wall={wall_ms:7.0f}ms  "
            f"p50={statistics.median(latencies_ms):7.1f}ms  max={max(latencies_ms):7.1f}ms"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db", required=True, type=Path, help="Path to a COPY of the database to bench"
    )
    parser.add_argument(
        "--seconds", type=float, default=20.0, help="Duration of the lock-occupancy sample"
    )
    parser.add_argument(
        "--copy-from",
        type=Path,
        default=None,
        help="Copy this file (+ -wal/-shm) to --db before benching, so the original is untouched",
    )
    args = parser.parse_args(argv)

    if args.copy_from is not None:
        for suffix in ("", "-wal", "-shm"):
            src = Path(f"{args.copy_from}{suffix}")
            if src.exists():
                shutil.copy(src, Path(f"{args.db}{suffix}"))
        print(f"copied {args.copy_from} (+ -wal/-shm) -> {args.db}")

    if not args.db.exists():
        print(f"error: {args.db} does not exist (pass --copy-from to seed it)", file=sys.stderr)
        return 2

    print(f"== bench target: {args.db} ({args.db.stat().st_size / 1024 / 1024:.0f} MiB) ==")

    print("\n== commit cost by synchronous level ==")
    probe_commit_cost(args.db, "FULL")
    probe_commit_cost(args.db, "NORMAL")

    probe_pool_concurrency(args.db, [5, 15, 40, 80])

    probe_lock_occupancy(args.db, args.seconds)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
