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

    assert revision == "0014_durable_learning_tasks"
    assert "uq_quiz_attempts_run_user_idempotency" in attempt_schema
    assert "uq_quiz_attempts_user_id_idempotency_key" not in attempt_schema
    assert quiz_columns["content_version_id"][3] == 1
    assert "uq_learning_tasks_run_type_idempotency" in learning_task_schema
