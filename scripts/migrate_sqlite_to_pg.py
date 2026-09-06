#!/usr/bin/env python
"""SQLite -> PostgreSQL data migration (task-75 §4).

Run against a COPY of the production database, never the live file
directly — reuse the freshest snapshot `scripts/backup_sqlite.py` already
took (`app_data/backups/app-<stamp>.db`), or take a fresh one. The target
is expected to be an EMPTY Postgres database (the `db` compose service,
task-75 §2) — this script creates the schema itself from `Base.metadata`,
it does not replay Alembic migrations (task-75 §5.2 explains why: the
future Postgres baseline is generated fresh from the models, not from the
31 SQLite-era migration files).

    docker compose exec api poetry run python scripts/migrate_sqlite_to_pg.py \
        --source-url sqlite:////app/app_data/backups/app-<stamp>.db \
        --target-url postgresql+psycopg://yovth:<password>@127.0.0.1:6532/yovth \
        --verify

Five steps, in order:
  1. `create_schema` — `Base.metadata.create_all()` on the target.
  2. `copy_all_tables` — defer FK checks for the duration
     (`session_replication_role = replica`), then stream every table in
     `Base.metadata.sorted_tables` order, chunked, so a 700k-row table
     never has to materialize in RAM at once.
  3. Re-enable FK checks (`session_replication_role = origin`) — this
     script does not additionally VALIDATE them; PostgreSQL enforces
     every FK on the very next write through it, which is enough for a
     dry run. A hard integrity guarantee before cutover is `--verify`'s
     job (below), not a second silent pass here.
  4. `align_sequences` — every integer-autoincrement primary key's
     Postgres sequence must start above the highest copied id, or the
     first INSERT after cutover collides on a duplicate key. A no-op for
     the overwhelming majority of tables: this schema keys almost
     everything with a GUID (Python-side default, no DB sequence at
     all) — `job_events.seq` is the one table where this actually does
     something (verified live, task-75 §4).
  5. `--verify`: `verify_row_counts` (every table) + `verify_checksums`
     (a short list of tables holding irreplaceable client data, not bulk
     metrics — row-by-row content comparison, not just row counts).
"""

from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import Column, Connection, Engine, Integer, Table, create_engine, func, select, text
from sqlalchemy.sql.elements import ColumnElement

from app.db import models  # noqa: F401 - registers tables on Base.metadata
from app.db.base import Base

DEFAULT_CHUNK_SIZE = 5000

# Tables worth a row-by-row content check beyond count(*) (task-75 §4 step
# 7): irreplaceable client data, not high-volume bulk metrics where a
# count match is already strong evidence and a full compare would be slow
# for no extra safety (gsc_dimension_metrics alone is 707k rows).
DEFAULT_CHECKSUM_TABLES = (
    "clients",
    "client_integrations",
    "metric_values",
    "provider_snapshots",
)


@dataclass
class MigrationReport:
    tables_copied: dict[str, int] = field(default_factory=dict)
    sequences_aligned: dict[str, int] = field(default_factory=dict)
    count_mismatches: list[tuple[str, int, int]] = field(default_factory=list)
    checksum_mismatches: list[str] = field(default_factory=list)
    elapsed_s: float = 0.0

    @property
    def total_rows(self) -> int:
        return sum(self.tables_copied.values())

    def ok(self) -> bool:
        return not self.count_mismatches and not self.checksum_mismatches


def create_schema(target: Engine) -> None:
    Base.metadata.create_all(target)


def _copy_one_table(source: Engine, target: Engine, table: Table, chunk_size: int) -> int:
    n = 0
    with source.connect() as sc, target.begin() as tc:
        result = sc.execution_options(stream_results=True, max_row_buffer=chunk_size).execute(
            select(table)
        )
        while True:
            rows = result.fetchmany(chunk_size)
            if not rows:
                break
            tc.execute(table.insert(), [dict(row._mapping) for row in rows])
            n += len(rows)
    return n


def copy_all_tables(
    source: Engine, target: Engine, *, chunk_size: int = DEFAULT_CHUNK_SIZE
) -> dict[str, int]:
    """Stream every table's rows source -> target, FK checks deferred for
    the duration so table order only needs to avoid re-reading a source
    table twice, not satisfy every FK on the way in.

    `session_replication_role` is Postgres-only — guarded by dialect name
    so this same function also runs (and is unit-tested) against a
    SQLite target, where SQLite's own `PRAGMA foreign_keys` semantics
    already tolerate this script's insertion order without it.
    """
    totals: dict[str, int] = {}
    tables = Base.metadata.sorted_tables
    is_postgres = target.dialect.name == "postgresql"
    if is_postgres:
        with target.begin() as conn:
            conn.exec_driver_sql("SET session_replication_role = replica")
    try:
        for table in tables:
            n = _copy_one_table(source, target, table, chunk_size)
            if n:
                totals[table.name] = n
    finally:
        if is_postgres:
            with target.begin() as conn:
                conn.exec_driver_sql("SET session_replication_role = origin")
    return totals


def integer_primary_key_columns() -> list[tuple[Table, Column[Any]]]:
    """Every `(table, column)` pair whose primary key is a real integer —
    the only kind of column that could have a Postgres-side sequence to
    align at all. Pure metadata inspection, no engine or dialect
    involved, which is what makes this worth unit-testing without a live
    Postgres: the schema's own regression here was calling
    `col.type.python_type` (raises `NotImplementedError` for every
    GUID-keyed table — `GUID`/`DecimalString`/`UTCDateTime` in
    `app/db/base.py` are `TypeDecorator`s that never override
    `python_type`) instead of `isinstance(col.type, Integer)`.
    """
    return [
        (table, col)
        for table in Base.metadata.sorted_tables
        for col in table.primary_key.columns
        if isinstance(col.type, Integer)
    ]


def align_sequences(target: Engine) -> dict[str, int]:
    """Push every integer-autoincrement PK's Postgres sequence past the
    highest copied id (task-75 §4 step 5). Silent trap otherwise: the
    schema and every row look perfect right up until the first INSERT
    after cutover collides on a duplicate key.

    A no-op for every GUID-keyed table — the overwhelming majority of
    this schema (`integer_primary_key_columns` above finds only one:
    `job_events.seq`, live-verified). Those have no DB-side sequence at
    all (id generated in Python at insert time via `new_uuid`), so
    `pg_get_serial_sequence` returns NULL and the loop skips them:
    nothing to align, not a missed case.

    `pg_get_serial_sequence`/`setval` are Postgres-only functions — a
    non-Postgres target (a SQLite target in this script's own unit
    tests) has nothing analogous to align, so this is a no-op there too.
    """
    aligned: dict[str, int] = {}
    if target.dialect.name != "postgresql":
        return aligned
    with target.begin() as conn:
        for table, col in integer_primary_key_columns():
            seq_name = conn.execute(
                text("SELECT pg_get_serial_sequence(:t, :c)"),
                {"t": table.name, "c": col.name},
            ).scalar()
            if seq_name is None:
                continue
            max_id = conn.execute(select(func.max(col))).scalar()
            if max_id is None:
                continue  # empty table, sequence already starts at 1
            conn.execute(text("SELECT setval(:seq, :val)"), {"seq": seq_name, "val": max_id})
            aligned[f"{table.name}.{col.name}"] = max_id
    return aligned


def verify_row_counts(source: Engine, target: Engine) -> list[tuple[str, int, int]]:
    mismatches: list[tuple[str, int, int]] = []
    with source.connect() as sc, target.connect() as tc:
        for table in Base.metadata.sorted_tables:
            a = sc.execute(select(func.count()).select_from(table)).scalar_one()
            b = tc.execute(select(func.count()).select_from(table)).scalar_one()
            if a != b:
                mismatches.append((table.name, a, b))
    return mismatches


def _rows_by_pk(
    connection: Connection, table: Table, pk_cols: Sequence[ColumnElement[Any]]
) -> dict[tuple[Any, ...], dict[str, Any]]:
    order = [c.asc() for c in pk_cols]
    result: dict[tuple[Any, ...], dict[str, Any]] = {}
    for row in connection.execute(select(table).order_by(*order)):
        mapping = row._mapping
        result[tuple(mapping[c.name] for c in pk_cols)] = dict(mapping)
    return result


def verify_checksums(
    source: Engine, target: Engine, table_names: tuple[str, ...] = DEFAULT_CHECKSUM_TABLES
) -> list[str]:
    """Row-by-row content compare on the tables that hold irreplaceable
    client data. `count(*)` alone only catches a missing or extra row —
    not one that copied with different bytes (e.g. a value that lost
    precision, or NULL vs empty string).
    """
    bad: list[str] = []
    tables_by_name = {t.name: t for t in Base.metadata.sorted_tables}
    with source.connect() as sc, target.connect() as tc:
        for name in table_names:
            table = tables_by_name[name]
            pk_cols = list(table.primary_key.columns)
            src_rows = _rows_by_pk(sc, table, pk_cols)
            dst_rows = _rows_by_pk(tc, table, pk_cols)
            if src_rows.keys() != dst_rows.keys():
                bad.append(
                    f"{name}: key sets differ (source={len(src_rows)}, target={len(dst_rows)})"
                )
                continue
            for key, src_row in src_rows.items():
                if src_row != dst_rows[key]:
                    bad.append(f"{name}: row {key} differs")
    return bad


def vacuum_analyze(target: Engine) -> None:
    """Postgres-only, and a no-op there too until the target actually has
    data worth planning around — refreshes `pg_statistic` for the query
    planner right after a bulk load, same reasoning as task-68 §1's
    `ANALYZE` (×4000 on one query, from stats that had simply never run).
    `VACUUM` (not just `ANALYZE`) also reclaims the dead-tuple bloat a
    single huge bulk INSERT can leave behind. Needs autocommit — `VACUUM`
    cannot run inside a transaction block.
    """
    if target.dialect.name != "postgresql":
        return
    with target.connect() as conn:
        conn.execution_options(isolation_level="AUTOCOMMIT").exec_driver_sql("VACUUM ANALYZE")


def migrate(
    source_url: str,
    target_url: str,
    *,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    skip_schema: bool = False,
    verify: bool = False,
    checksum_tables: tuple[str, ...] = DEFAULT_CHECKSUM_TABLES,
) -> MigrationReport:
    source = create_engine(source_url)
    target = create_engine(target_url)
    report = MigrationReport()
    t0 = time.perf_counter()
    try:
        if not skip_schema:
            create_schema(target)
        report.tables_copied = copy_all_tables(source, target, chunk_size=chunk_size)
        report.sequences_aligned = align_sequences(target)
        vacuum_analyze(target)
        if verify:
            report.count_mismatches = verify_row_counts(source, target)
            report.checksum_mismatches = verify_checksums(source, target, checksum_tables)
    finally:
        report.elapsed_s = time.perf_counter() - t0
        source.dispose()
        target.dispose()
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="migrate_sqlite_to_pg")
    parser.add_argument(
        "--source-url",
        required=True,
        help="sqlite:///<path to a COPY of app.db> — never the live file",
    )
    parser.add_argument(
        "--target-url",
        required=True,
        help="postgresql+psycopg://user:pass@host:port/db — expected empty",
    )
    parser.add_argument("--chunk-size", type=int, default=DEFAULT_CHUNK_SIZE)
    parser.add_argument(
        "--skip-schema",
        action="store_true",
        help="target already has the schema (e.g. re-running after a failed copy)",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="row-count reconciliation on every table + row-by-row checksum on "
        f"{', '.join(DEFAULT_CHECKSUM_TABLES)}",
    )
    args = parser.parse_args(argv)

    if "sqlite" not in args.source_url:
        print(f"--source-url must be a sqlite:// URL, got: {args.source_url!r}", file=sys.stderr)
        return 2

    report = migrate(
        args.source_url,
        args.target_url,
        chunk_size=args.chunk_size,
        skip_schema=args.skip_schema,
        verify=args.verify,
    )

    print(f"скопійовано {len(report.tables_copied)} таблиць, {report.total_rows} рядків, "
          f"{report.elapsed_s:.1f}с")
    for seq, val in sorted(report.sequences_aligned.items()):
        print(f"  sequence вирівняно -> {seq} = {val}")
    if not report.sequences_aligned:
        print("  sequence вирівнювати не було потреби (лише GUID-ключі)")

    if args.verify:
        if report.count_mismatches:
            print("РОЗБІЖНОСТІ У КІЛЬКОСТІ РЯДКІВ:", file=sys.stderr)
            for name, a, b in report.count_mismatches:
                print(f"  {name}: source={a} target={b}", file=sys.stderr)
        else:
            print("кількість рядків: збігається у ВСІХ таблицях")

        if report.checksum_mismatches:
            print("РОЗБІЖНОСТІ У ВМІСТІ:", file=sys.stderr)
            for line in report.checksum_mismatches:
                print(f"  {line}", file=sys.stderr)
        else:
            print("вміст (checksum-таблиці): збігається побітово")

    return 0 if report.ok() else 1


if __name__ == "__main__":
    sys.exit(main())
