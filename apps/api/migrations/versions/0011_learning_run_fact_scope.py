"""Scope learning facts and sessions to a learning run.

Revision ID: 0011_learning_run_fact_scope
Revises: 0010_user_progress_authority
"""

from alembic import op
import sqlalchemy as sa


revision = "0011_learning_run_fact_scope"
down_revision = "0010_user_progress_authority"
branch_labels = None
depends_on = None

NAMING_CONVENTION = {"uq": "uq_%(table_name)s_%(column_0_name)s"}


def _columns(connection, table_name):
    return {
        item["name"]
        for item in sa.inspect(connection).get_columns(table_name)
    }


def _indexes(connection, table_name):
    return {
        item["name"]
        for item in sa.inspect(connection).get_indexes(table_name)
    }


def _add_run_column(connection, table_name):
    if "learning_run_id" not in _columns(connection, table_name):
        op.add_column(
            table_name,
            sa.Column("learning_run_id", sa.String(), nullable=True),
        )
        op.create_index(
            f"ix_{table_name}_learning_run_id",
            table_name,
            ["learning_run_id"],
            unique=False,
        )


def _backfill_by_section(connection, table_name):
    connection.execute(
        sa.text(
            f"""
            UPDATE {table_name}
            SET learning_run_id = (
                SELECT lr.id
                FROM sections AS sec
                JOIN chapters AS ch ON ch.id = sec.chapter_id
                JOIN books AS b ON b.id = ch.book_id
                JOIN learning_runs AS lr ON lr.series_id = b.series_id
                WHERE sec.id = {table_name}.section_id
                  AND lr.user_id = {table_name}.user_id
                  AND lr.status = 'active'
                ORDER BY lr.created_at DESC
                LIMIT 1
            )
            WHERE learning_run_id IS NULL
            """
        )
    )


def upgrade():
    connection = op.get_bind()

    # A user/series pair has exactly one active run. Preserve the newest row if
    # an earlier compatibility path created duplicates.
    rows = connection.execute(
        sa.text(
            """
            SELECT id, user_id, series_id
            FROM learning_runs
            WHERE status = 'active'
            ORDER BY created_at DESC, id DESC
            """
        )
    ).all()
    seen = set()
    for row in rows:
        key = (row.user_id, row.series_id)
        if key in seen:
            connection.execute(
                sa.text(
                    """
                    UPDATE learning_runs
                    SET status = 'superseded', completed_at = CURRENT_TIMESTAMP
                    WHERE id = :id
                    """
                ),
                {"id": row.id},
            )
        else:
            seen.add(key)
    if "uq_learning_runs_active_user_series" not in _indexes(
        connection,
        "learning_runs",
    ):
        op.create_index(
            "uq_learning_runs_active_user_series",
            "learning_runs",
            ["user_id", "series_id"],
            unique=True,
            sqlite_where=sa.text("status = 'active'"),
            postgresql_where=sa.text("status = 'active'"),
        )

    if "version" not in _columns(connection, "section_progress"):
        op.add_column(
            "section_progress",
            sa.Column(
                "version",
                sa.Integer(),
                nullable=False,
                server_default="1",
            ),
        )

    for table_name in [
        "quiz_attempts",
        "qa_sessions",
        "learning_notes",
        "note_generation_tasks",
        "learning_evidence",
        "ask_me_sessions",
        "artifact_attachments",
    ]:
        _add_run_column(connection, table_name)

    attempt_columns = _columns(connection, "quiz_attempts")
    if "workflow_status" not in attempt_columns:
        op.add_column(
            "quiz_attempts",
            sa.Column(
                "workflow_status",
                sa.String(length=24),
                nullable=False,
                server_default="completed",
            ),
        )
    if "response_json" not in attempt_columns:
        op.add_column(
            "quiz_attempts",
            sa.Column(
                "response_json",
                sa.Text(),
                nullable=False,
                server_default="",
            ),
        )
    if "workflow_error_code" not in attempt_columns:
        op.add_column(
            "quiz_attempts",
            sa.Column(
                "workflow_error_code",
                sa.String(length=80),
                nullable=False,
                server_default="",
            ),
        )

    connection.execute(
        sa.text(
            """
            UPDATE quiz_attempts
            SET learning_run_id = (
                SELECT lr.id
                FROM quiz_sets AS q
                JOIN sections AS sec ON sec.id = q.section_id
                JOIN chapters AS ch ON ch.id = sec.chapter_id
                JOIN books AS b ON b.id = ch.book_id
                JOIN learning_runs AS lr ON lr.series_id = b.series_id
                WHERE q.id = quiz_attempts.quiz_set_id
                  AND lr.user_id = quiz_attempts.user_id
                  AND lr.status = 'active'
                ORDER BY lr.created_at DESC
                LIMIT 1
            )
            WHERE learning_run_id IS NULL
            """
        )
    )
    connection.execute(
        sa.text(
            """
            UPDATE learning_evidence
            SET learning_run_id = (
                SELECT lr.id
                FROM learning_runs AS lr
                WHERE lr.series_id = learning_evidence.series_id
                  AND lr.user_id = learning_evidence.user_id
                  AND lr.status = 'active'
                ORDER BY lr.created_at DESC
                LIMIT 1
            )
            WHERE learning_run_id IS NULL
            """
        )
    )
    for table_name in ["qa_sessions", "learning_notes", "ask_me_sessions"]:
        _backfill_by_section(connection, table_name)
    connection.execute(
        sa.text(
            """
            UPDATE note_generation_tasks
            SET learning_run_id = (
                SELECT qa.learning_run_id
                FROM quiz_attempts AS qa
                WHERE qa.id = note_generation_tasks.trigger_attempt_id
            )
            WHERE learning_run_id IS NULL
            """
        )
    )
    connection.execute(
        sa.text(
            """
            UPDATE artifact_attachments
            SET learning_run_id = (
                SELECT lr.id
                FROM learning_runs AS lr
                JOIN books AS b ON b.series_id = lr.series_id
                LEFT JOIN book_capstones AS bc
                  ON bc.book_id = b.id
                 AND artifact_attachments.target_type = 'book_capstone'
                LEFT JOIN chapters AS ch ON ch.book_id = b.id
                LEFT JOIN chapter_practices AS cp
                  ON cp.chapter_id = ch.id
                 AND artifact_attachments.target_type = 'chapter_practice'
                WHERE lr.user_id = artifact_attachments.user_id
                  AND lr.status = 'active'
                  AND (
                    bc.id = artifact_attachments.target_id
                    OR cp.id = artifact_attachments.target_id
                  )
                ORDER BY lr.created_at DESC
                LIMIT 1
            )
            WHERE learning_run_id IS NULL
            """
        )
    )

    for table_name in [
        "quiz_attempts",
        "qa_sessions",
        "learning_notes",
        "note_generation_tasks",
        "learning_evidence",
        "ask_me_sessions",
        "artifact_attachments",
    ]:
        missing = connection.execute(
            sa.text(
                f"SELECT COUNT(*) FROM {table_name} "
                "WHERE learning_run_id IS NULL"
            )
        ).scalar_one()
        if missing:
            raise RuntimeError(
                f"{table_name} has {missing} rows without a learning run"
            )

    constraint_rewrites = [
        (
            "quiz_attempts",
            "uq_quiz_attempts_user_id_idempotency_key",
            "uq_quiz_attempts_run_user_idempotency",
            ["learning_run_id", "user_id", "idempotency_key"],
        ),
        (
            "qa_sessions",
            "uq_qa_sessions_section_id_user_id",
            "uq_qa_sessions_run_section_user",
            ["learning_run_id", "section_id", "user_id"],
        ),
        (
            "learning_notes",
            "uq_learning_notes_section_id_user_id",
            "uq_learning_notes_run_section_user",
            ["learning_run_id", "section_id", "user_id"],
        ),
        (
            "ask_me_sessions",
            "uq_ask_me_sessions_section_id",
            "uq_ask_me_sessions_run_section_user",
            ["learning_run_id", "section_id", "user_id"],
        ),
    ]
    for table_name, old_name, new_name, columns in constraint_rewrites:
        uniques = sa.inspect(connection).get_unique_constraints(table_name)
        if any(item["name"] == new_name for item in uniques):
            continue
        actual_old_name = next(
            (
                item["name"]
                for item in uniques
                if item["name"]
                and (
                    item["name"] == old_name
                    or tuple(item["column_names"]) == tuple(columns[1:])
                )
            ),
            old_name,
        )
        if connection.dialect.name == "postgresql":
            # Recreating a referenced table would temporarily drop its primary
            # key, which PostgreSQL correctly rejects. These changes are native
            # ALTER TABLE operations and do not require a table copy.
            op.drop_constraint(actual_old_name, table_name, type_="unique")
            op.alter_column(
                table_name,
                "learning_run_id",
                existing_type=sa.String(),
                nullable=False,
            )
            op.create_unique_constraint(new_name, table_name, columns)
            continue
        with op.batch_alter_table(
            table_name,
            recreate="always",
            naming_convention=NAMING_CONVENTION,
        ) as batch_op:
            batch_op.drop_constraint(actual_old_name, type_="unique")
            batch_op.alter_column(
                "learning_run_id",
                existing_type=sa.String(),
                nullable=False,
            )
            batch_op.create_unique_constraint(new_name, columns)

    for table_name in [
        "note_generation_tasks",
        "learning_evidence",
        "artifact_attachments",
    ]:
        with op.batch_alter_table(table_name) as batch_op:
            batch_op.alter_column(
                "learning_run_id",
                existing_type=sa.String(),
                nullable=False,
            )


def downgrade():
    # Downgrade intentionally retains fact rows while removing only the new
    # scoping columns and workflow metadata.
    for table_name in [
        "artifact_attachments",
        "ask_me_sessions",
        "learning_evidence",
        "note_generation_tasks",
        "learning_notes",
        "qa_sessions",
        "quiz_attempts",
    ]:
        if "learning_run_id" in _columns(op.get_bind(), table_name):
            with op.batch_alter_table(table_name) as batch_op:
                batch_op.drop_column("learning_run_id")
    for column in ["workflow_error_code", "response_json", "workflow_status"]:
        if column in _columns(op.get_bind(), "quiz_attempts"):
            with op.batch_alter_table("quiz_attempts") as batch_op:
                batch_op.drop_column(column)
    if "version" in _columns(op.get_bind(), "section_progress"):
        with op.batch_alter_table("section_progress") as batch_op:
            batch_op.drop_column("version")
    if "uq_learning_runs_active_user_series" in _indexes(
        op.get_bind(),
        "learning_runs",
    ):
        op.drop_index(
            "uq_learning_runs_active_user_series",
            table_name="learning_runs",
        )
