import os
from pathlib import Path
import sqlite3
import subprocess


API_ROOT = Path(__file__).resolve().parents[1]


def test_fresh_database_migrates_to_head_with_run_scoped_idempotency(tmp_path):
    database = tmp_path / "fresh.db"
    environment = {
        **os.environ,
        "DATABASE_URL": f"sqlite+pysqlite:///{database}",
        "PYTHONPATH": ".",
    }
    subprocess.run(
        ["../../.venv/bin/alembic", "upgrade", "head"],
        cwd=API_ROOT,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )

    with sqlite3.connect(database) as connection:
        revision = connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()[0]
        attempt_schema = connection.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type = 'table' AND name = 'quiz_attempts'"
        ).fetchone()[0]
        quiz_columns = {
            row[1]: row
            for row in connection.execute("PRAGMA table_info(quiz_sets)")
        }
        learning_task_schema = connection.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type = 'table' AND name = 'learning_tasks'"
        ).fetchone()[0]
        learning_task_columns = {
            row[1]: row
            for row in connection.execute("PRAGMA table_info(learning_tasks)")
        }
        generation_lease_schema = connection.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type = 'table' AND name = 'generation_leases'"
        ).fetchone()[0]

    assert revision == "0017_ai_invocation_ledger"
    assert "uq_quiz_attempts_run_user_idempotency" in attempt_schema
    assert "uq_quiz_attempts_user_id_idempotency_key" not in attempt_schema
    assert quiz_columns["content_version_id"][3] == 1
    assert "uq_learning_tasks_run_type_idempotency" in learning_task_schema
    assert learning_task_columns["section_id"][3] == 0
    assert "resource_key" in generation_lease_schema
    assert "UNIQUE (owner_id)" in generation_lease_schema
    with sqlite3.connect(database) as connection:
        invocation_columns = {
            row[1]: row
            for row in connection.execute("PRAGMA table_info(ai_invocations)")
        }
        measurement_schema = connection.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type = 'table' AND name = 'ai_usage_measurements'"
        ).fetchone()[0]
    assert invocation_columns["subject_user_id"][3] == 0
    assert "uq_ai_usage_measurement_source_version" in measurement_schema


def test_generation_lease_migration_accepts_orm_precreated_table(tmp_path):
    database = tmp_path / "precreated-lease.db"
    environment = {
        **os.environ,
        "DATABASE_URL": f"sqlite+pysqlite:///{database}",
        "PYTHONPATH": ".",
    }
    subprocess.run(
        ["../../.venv/bin/alembic", "upgrade", "0015_initial_lesson_preload"],
        cwd=API_ROOT,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            CREATE TABLE generation_leases (
                resource_key VARCHAR(200) NOT NULL PRIMARY KEY,
                owner_id VARCHAR(80) NOT NULL UNIQUE,
                acquired_at DATETIME NOT NULL,
                expires_at DATETIME NOT NULL
            )
            """
        )
        connection.execute(
            "CREATE INDEX ix_generation_leases_expires_at "
            "ON generation_leases (expires_at)"
        )

    subprocess.run(
        ["../../.venv/bin/alembic", "upgrade", "head"],
        cwd=API_ROOT,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    with sqlite3.connect(database) as connection:
        revision = connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()[0]
    assert revision == "0017_ai_invocation_ledger"
