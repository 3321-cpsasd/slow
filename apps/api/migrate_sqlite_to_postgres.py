"""One-way, fail-closed migration from Slow's SQLite authority to PostgreSQL.

The target schema must already be migrated to the same Alembic revision and
must contain no application rows.  The copy runs in one PostgreSQL transaction;
any schema, count, foreign-key, or digest mismatch rolls the whole import back.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from typing import Any, Iterator

from alembic.config import Config
from alembic.script import ScriptDirectory
import sqlalchemy as sa
from sqlalchemy.engine import Connection, Engine, URL
from sqlalchemy.pool import NullPool

from app.infrastructure.tables import Base


API_ROOT = Path(__file__).resolve().parent
ALEMBIC_TABLE = "alembic_version"
MIGRATION_LOCK_KEY = "slow-sqlite-to-postgresql-v1"
SCHEMA_BOOTSTRAP_ROWS = {
    "bkt_parameter_activation_events": (
        "id",
        {"bkt_activation_system_default_v2"},
    ),
    "bkt_parameter_set_versions": (
        "version",
        {"bkt_multimodal_v2"},
    ),
}


class MigrationRefused(RuntimeError):
    """Raised when the cutover preconditions are not safe."""


def _expected_revision() -> str:
    config = Config(str(API_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(API_ROOT / "migrations"))
    revision = ScriptDirectory.from_config(config).get_current_head()
    if not revision:
        raise MigrationRefused("Alembic migration history has no single head")
    return revision


def _database_revision(connection: Connection) -> str | None:
    inspector = sa.inspect(connection)
    if ALEMBIC_TABLE not in inspector.get_table_names():
        return None
    return connection.execute(
        sa.text("SELECT version_num FROM alembic_version")
    ).scalar_one_or_none()


@contextmanager
def _sqlite_snapshot(source_path: Path) -> Iterator[Path]:
    source_path = source_path.expanduser().resolve()
    if not source_path.is_file():
        raise MigrationRefused(f"SQLite source does not exist: {source_path}")
    with tempfile.TemporaryDirectory(prefix="slow-sqlite-snapshot-") as directory:
        snapshot_path = Path(directory) / "slow.db"
        source_uri = f"file:{source_path.as_posix()}?mode=ro"
        with sqlite3.connect(source_uri, uri=True) as source:
            with sqlite3.connect(snapshot_path) as snapshot:
                source.backup(snapshot)
        yield snapshot_path


def _application_tables(connection: Connection) -> set[str]:
    return set(sa.inspect(connection).get_table_names()) - {ALEMBIC_TABLE}


def _assert_schema(
    source: Connection,
    target: Connection,
    expected_revision: str,
) -> list[str]:
    source_revision = _database_revision(source)
    target_revision = _database_revision(target)
    if source_revision != expected_revision:
        raise MigrationRefused(
            f"SQLite revision is {source_revision!r}; expected {expected_revision!r}"
        )
    if target_revision != expected_revision:
        raise MigrationRefused(
            f"PostgreSQL revision is {target_revision!r}; expected {expected_revision!r}"
        )

    model_tables = set(Base.metadata.tables)
    source_tables = _application_tables(source)
    target_tables = _application_tables(target)
    if source_tables != model_tables:
        missing = sorted(model_tables - source_tables)
        extra = sorted(source_tables - model_tables)
        raise MigrationRefused(
            f"SQLite schema differs from the model; missing={missing}, extra={extra}"
        )
    if target_tables != model_tables:
        missing = sorted(model_tables - target_tables)
        extra = sorted(target_tables - model_tables)
        raise MigrationRefused(
            f"PostgreSQL schema differs from the model; missing={missing}, extra={extra}"
        )
    return [table.name for table in Base.metadata.sorted_tables]


def _prepare_empty_target(connection: Connection, table_names: list[str]) -> None:
    """Accept only migration-owned bootstrap rows, then restore a truly empty target.

    Some schema revisions install frozen system defaults. They are part of the
    schema bootstrap rather than user/application data, and the authoritative
    SQLite snapshot contains its own copy. Any row outside the exact bootstrap
    primary keys still fails closed.
    """
    metadata = sa.MetaData()
    metadata.reflect(bind=connection, only=table_names)
    populated = []
    for table_name in table_names:
        table = metadata.tables[table_name]
        bootstrap = SCHEMA_BOOTSTRAP_ROWS.get(table_name)
        if bootstrap:
            key_name, allowed_keys = bootstrap
            actual_keys = set(connection.execute(sa.select(table.c[key_name])).scalars())
            if actual_keys <= allowed_keys:
                continue
        if connection.execute(
            sa.select(sa.literal(1)).select_from(table).limit(1)
        ).first():
            populated.append(table_name)
    if populated:
        raise MigrationRefused(
            "PostgreSQL target is not empty; refusing to overwrite tables: "
            + ", ".join(populated)
        )

    # Delete children before parents. The transaction restores these rows from
    # the authoritative SQLite snapshot or rolls back the entire import.
    for table_name in SCHEMA_BOOTSTRAP_ROWS:
        if table_name in metadata.tables:
            connection.execute(metadata.tables[table_name].delete())


def _coerce_value(value: Any, column: sa.Column[Any]) -> Any:
    if value is None:
        return None
    if isinstance(column.type, sa.Boolean):
        return bool(value)
    if isinstance(column.type, sa.DateTime):
        if isinstance(value, str):
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if isinstance(value, datetime) and column.type.timezone:
            if value.tzinfo is None:
                return value.replace(tzinfo=timezone.utc)
            return value.astimezone(timezone.utc)
    if isinstance(column.type, sa.Date) and not isinstance(value, datetime):
        if isinstance(value, str):
            return date.fromisoformat(value)
    return value


def _coerce_row(row: dict[str, Any], target_table: sa.Table) -> dict[str, Any]:
    return {
        column.name: _coerce_value(row[column.name], column)
        for column in target_table.columns
    }


def _canonical(value: Any) -> Any:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, bytes):
        return {"bytesHex": value.hex()}
    return value


def _table_digest(
    connection: Connection,
    read_table: sa.Table,
    target_table: sa.Table,
) -> tuple[int, str]:
    primary_key = list(read_table.primary_key.columns)
    if not primary_key:
        raise MigrationRefused(f"Table {read_table.name} has no primary key")
    digest = hashlib.sha256()
    count = 0
    rows = connection.execute(sa.select(read_table).order_by(*primary_key)).mappings()
    for row in rows:
        normalized = _coerce_row(dict(row), target_table)
        payload = {
            key: _canonical(value)
            for key, value in sorted(normalized.items())
        }
        digest.update(
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        digest.update(b"\n")
        count += 1
    return count, digest.hexdigest()


def _reflected_tables(
    connection: Connection,
    table_names: list[str],
) -> dict[str, sa.Table]:
    metadata = sa.MetaData()
    metadata.reflect(bind=connection, only=table_names)
    return {name: metadata.tables[name] for name in table_names}


def _verify_tables(
    source: Connection,
    target: Connection,
    table_names: list[str],
) -> dict[str, int]:
    source_tables = _reflected_tables(source, table_names)
    target_tables = _reflected_tables(target, table_names)
    counts: dict[str, int] = {}
    for table_name in table_names:
        source_result = _table_digest(
            source,
            source_tables[table_name],
            target_tables[table_name],
        )
        target_result = _table_digest(
            target,
            target_tables[table_name],
            target_tables[table_name],
        )
        if source_result != target_result:
            raise MigrationRefused(
                f"Verification mismatch for table {table_name}: "
                f"source_count={source_result[0]}, target_count={target_result[0]}"
            )
        counts[table_name] = source_result[0]
    return counts


def _copy_tables(
    source: Connection,
    target: Connection,
    table_names: list[str],
    batch_size: int = 500,
) -> None:
    source_tables = _reflected_tables(source, table_names)
    target_tables = _reflected_tables(target, table_names)
    for table_name in table_names:
        source_table = source_tables[table_name]
        target_table = target_tables[table_name]
        primary_key = list(source_table.primary_key.columns)
        rows = source.execute(
            sa.select(source_table).order_by(*primary_key)
        ).mappings()
        batch: list[dict[str, Any]] = []
        for row in rows:
            batch.append(_coerce_row(dict(row), target_table))
            if len(batch) >= batch_size:
                target.execute(target_table.insert(), batch)
                batch.clear()
        if batch:
            target.execute(target_table.insert(), batch)


def _synchronize_postgresql_sequences(
    target: Connection,
    table_names: list[str],
) -> None:
    """Advance serial/identity sequences after copying explicit column values."""
    target_tables = _reflected_tables(target, table_names)
    for table_name in table_names:
        table = target_tables[table_name]
        for column in table.columns:
            sequence_name = target.execute(
                sa.text(
                    "SELECT pg_get_serial_sequence(:table_name, :column_name)"
                ),
                {
                    "table_name": table.fullname,
                    "column_name": column.name,
                },
            ).scalar_one()
            if not sequence_name:
                continue

            maximum = target.execute(
                sa.select(sa.func.max(column))
            ).scalar_one()
            target.execute(
                sa.text(
                    "SELECT setval("
                    "to_regclass(:sequence_name), :value, :is_called"
                    ")"
                ),
                {
                    "sequence_name": sequence_name,
                    "value": int(maximum) if maximum is not None else 1,
                    "is_called": maximum is not None,
                },
            )


def migrate(
    source_path: Path,
    target_url: str,
    *,
    verify_only: bool = False,
) -> dict[str, int]:
    target_engine: Engine = sa.create_engine(target_url, poolclass=NullPool)
    if target_engine.dialect.name != "postgresql":
        target_engine.dispose()
        raise MigrationRefused("Target DATABASE_URL must use PostgreSQL")

    expected_revision = _expected_revision()
    try:
        with _sqlite_snapshot(source_path) as snapshot_path:
            source_engine = sa.create_engine(
                URL.create("sqlite+pysqlite", database=str(snapshot_path)),
                poolclass=NullPool,
            )
            try:
                with source_engine.connect() as source:
                    violations = source.exec_driver_sql(
                        "PRAGMA foreign_key_check"
                    ).fetchmany(10)
                    if violations:
                        raise MigrationRefused(
                            f"SQLite foreign-key violations detected: {violations}"
                        )
                    with target_engine.begin() as target:
                        target.execute(sa.text("SET LOCAL TIME ZONE 'UTC'"))
                        target.execute(
                            sa.text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
                            {"key": MIGRATION_LOCK_KEY},
                        )
                        table_names = _assert_schema(
                            source,
                            target,
                            expected_revision,
                        )
                        if not verify_only:
                            _prepare_empty_target(target, table_names)
                            _copy_tables(source, target, table_names)
                        counts = _verify_tables(
                            source,
                            target,
                            table_names,
                        )
                        if not verify_only:
                            _synchronize_postgresql_sequences(target, table_names)
                        return counts
            finally:
                source_engine.dispose()
    finally:
        target_engine.dispose()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Copy an upgraded Slow SQLite database into an empty PostgreSQL schema."
    )
    parser.add_argument("sqlite_path", type=Path)
    parser.add_argument(
        "--target-env",
        default="DATABASE_URL",
        help="Environment variable containing the PostgreSQL URL (default: DATABASE_URL).",
    )
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="Compare an already imported PostgreSQL database without writing.",
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    target_url = os.environ.get(args.target_env, "").strip()
    if not target_url:
        raise SystemExit(f"Missing PostgreSQL URL in {args.target_env}")
    try:
        counts = migrate(
            args.sqlite_path,
            target_url,
            verify_only=args.verify_only,
        )
    except MigrationRefused as error:
        raise SystemExit(f"Migration refused: {error}") from error
    print(
        json.dumps(
            {
                "ok": True,
                "mode": "verify" if args.verify_only else "migrate",
                "tables": len(counts),
                "rows": sum(counts.values()),
            },
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
