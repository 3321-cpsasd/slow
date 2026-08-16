from __future__ import annotations

from pathlib import Path
import os
import subprocess
import sys

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import make_url

from app.infrastructure.database import build_database
from app.infrastructure.tables import Base, LearningRun, Shelf, User
from migrate_sqlite_to_postgres import (
    MigrationRefused,
    SCHEMA_BOOTSTRAP_ROWS,
    _synchronize_postgresql_sequences,
    migrate,
)


API_ROOT = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.postgresql


def _upgrade(url: str, revision: str = "head") -> None:
    environment = os.environ.copy()
    environment["DATABASE_URL"] = url
    subprocess.run(
        [
            sys.executable,
            "-m",
            "alembic",
            "-c",
            "alembic.ini",
            "upgrade",
            revision,
        ],
        cwd=API_ROOT,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )


def test_postgresql_upgrades_existing_schema_from_previous_head():
    target_url = os.environ.get("POSTGRES_TEST_DATABASE_URL", "").strip()
    if not target_url:
        pytest.skip("POSTGRES_TEST_DATABASE_URL is not configured")
    parsed = make_url(target_url)
    assert parsed.get_backend_name() == "postgresql"
    assert parsed.database == "slow_test", (
        "integration test requires disposable slow_test"
    )

    migration_database = "slow_migration_test"
    admin_url = parsed.set(database="postgres").render_as_string(
        hide_password=False
    )
    migration_url = parsed.set(database=migration_database).render_as_string(
        hide_password=False
    )
    admin_engine = sa.create_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        with admin_engine.connect() as connection:
            connection.exec_driver_sql(
                f'DROP DATABASE IF EXISTS "{migration_database}" WITH (FORCE)'
            )
            connection.exec_driver_sql(
                f'CREATE DATABASE "{migration_database}"'
            )

        _upgrade(migration_url, "0064_shelf_soft_delete")
        migration_engine = sa.create_engine(migration_url)
        try:
            with migration_engine.connect() as connection:
                assert connection.execute(
                    sa.text("SELECT version_num FROM alembic_version")
                ).scalar_one() == "0064_shelf_soft_delete"
                assert "reading_annotations" not in sa.inspect(
                    connection
                ).get_table_names()
        finally:
            migration_engine.dispose()

        _upgrade(migration_url)
        migration_engine = sa.create_engine(migration_url)
        try:
            with migration_engine.connect() as connection:
                assert connection.execute(
                    sa.text("SELECT version_num FROM alembic_version")
                ).scalar_one() == "0074_merge_capabilities_annotations"
                inspector = sa.inspect(connection)
                assert "reading_annotations" in inspector.get_table_names()
                assert "display_title" in {
                    column["name"] for column in inspector.get_columns("series")
                }
                assert "created_at" in {
                    column["name"]
                    for column in inspector.get_columns("qa_sessions")
                }
        finally:
            migration_engine.dispose()
    finally:
        with admin_engine.connect() as connection:
            connection.exec_driver_sql(
                f'DROP DATABASE IF EXISTS "{migration_database}" WITH (FORCE)'
            )
        admin_engine.dispose()


def test_active_learning_run_index_is_partial_on_both_databases():
    index = next(
        item
        for item in LearningRun.__table__.indexes
        if item.name == "uq_learning_runs_active_user_series"
    )

    assert index.dialect_options["sqlite"]["where"] is not None
    assert index.dialect_options["postgresql"]["where"] is not None


def test_sqlite_to_postgresql_import_and_nonempty_refusal(tmp_path):
    target_url = os.environ.get("POSTGRES_TEST_DATABASE_URL", "").strip()
    if not target_url:
        pytest.skip("POSTGRES_TEST_DATABASE_URL is not configured")
    parsed = make_url(target_url)
    assert parsed.get_backend_name() == "postgresql"
    assert parsed.database == "slow_test", "integration test requires disposable slow_test"

    _upgrade(target_url)
    target_engine = sa.create_engine(target_url)
    try:
        target_tables = set(sa.inspect(target_engine).get_table_names())
        assert set(Base.metadata.tables) <= target_tables
        with target_engine.connect() as connection:
            populated = [
                table.name
                for table in Base.metadata.sorted_tables
                if connection.execute(
                    sa.select(sa.literal(1)).select_from(table).limit(1)
                ).first()
            ]
        assert set(populated) == set(SCHEMA_BOOTSTRAP_ROWS), (
            f"PostgreSQL test database has non-bootstrap rows: {populated}"
        )
    finally:
        target_engine.dispose()

    sqlite_path = tmp_path / "source.db"
    sqlite_url = f"sqlite+pysqlite:///{sqlite_path}"
    _upgrade(sqlite_url)
    source_engine, _ = build_database(sqlite_url)
    try:
        with source_engine.begin() as connection:
            connection.execute(
                User.__table__.insert().values(id="user_pg_import", name="迁移用户")
            )
            connection.execute(
                Shelf.__table__.insert().values(
                    id="shelf_pg_import",
                    user_id="user_pg_import",
                    name="数据库",
                    domain="software-engineering",
                )
            )
    finally:
        source_engine.dispose()

    counts = migrate(sqlite_path, target_url)
    assert counts["users"] == 1
    assert counts["shelves"] == 1

    target_engine = sa.create_engine(target_url)
    try:
        with target_engine.connect() as connection:
            assert connection.execute(
                sa.select(User.name).where(User.id == "user_pg_import")
            ).scalar_one() == "迁移用户"
            assert connection.execute(
                sa.select(Shelf.user_id).where(Shelf.id == "shelf_pg_import")
            ).scalar_one() == "user_pg_import"
    finally:
        target_engine.dispose()

    with pytest.raises(MigrationRefused, match="target is not empty"):
        migrate(sqlite_path, target_url)


def test_postgresql_sequence_sync_for_populated_and_empty_tables():
    target_url = os.environ.get("POSTGRES_TEST_DATABASE_URL", "").strip()
    if not target_url:
        pytest.skip("POSTGRES_TEST_DATABASE_URL is not configured")
    parsed = make_url(target_url)
    assert parsed.get_backend_name() == "postgresql"
    assert parsed.database == "slow_test", (
        "integration test requires disposable slow_test"
    )

    target_engine = sa.create_engine(target_url)
    try:
        with target_engine.begin() as connection:
            connection.exec_driver_sql("DROP TABLE IF EXISTS sequence_sync_probe")
            connection.exec_driver_sql(
                "CREATE TABLE sequence_sync_probe ("
                "sequence INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY, "
                "payload TEXT NOT NULL"
                ")"
            )
            connection.exec_driver_sql(
                "INSERT INTO sequence_sync_probe (sequence, payload) "
                "VALUES (7, 'imported')"
            )
            _synchronize_postgresql_sequences(
                connection,
                ["sequence_sync_probe"],
            )
            inserted = connection.exec_driver_sql(
                "INSERT INTO sequence_sync_probe (payload) VALUES ('generated') "
                "RETURNING sequence"
            ).scalar_one()
            assert inserted == 8

            connection.exec_driver_sql("TRUNCATE sequence_sync_probe")
            _synchronize_postgresql_sequences(
                connection,
                ["sequence_sync_probe"],
            )
            first = connection.exec_driver_sql(
                "INSERT INTO sequence_sync_probe (payload) VALUES ('first') "
                "RETURNING sequence"
            ).scalar_one()
            assert first == 1
            connection.exec_driver_sql("DROP TABLE sequence_sync_probe")
    finally:
        target_engine.dispose()
