"""Add a shared durable queue for post-quiz AI work.

Revision ID: 0014_durable_learning_tasks
Revises: 0013_learning_run_fks
"""

from alembic import op
import sqlalchemy as sa


revision = "0014_durable_learning_tasks"
down_revision = "0013_learning_run_fks"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "learning_tasks",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("learning_run_id", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("section_id", sa.String(), nullable=False),
        sa.Column("task_type", sa.String(length=40), nullable=False),
        sa.Column("idempotency_key", sa.String(length=160), nullable=False),
        sa.Column("trigger_id", sa.String(length=160), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("result_json", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("error_code", sa.String(length=80), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["learning_run_id"], ["learning_runs.id"]),
        sa.ForeignKeyConstraint(["section_id"], ["sections.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "learning_run_id",
            "task_type",
            "idempotency_key",
            name="uq_learning_tasks_run_type_idempotency",
        ),
    )
    op.create_index(
        op.f("ix_learning_tasks_learning_run_id"),
        "learning_tasks",
        ["learning_run_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_learning_tasks_section_id"),
        "learning_tasks",
        ["section_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_learning_tasks_status"),
        "learning_tasks",
        ["status"],
        unique=False,
    )
    op.create_index(
        op.f("ix_learning_tasks_task_type"),
        "learning_tasks",
        ["task_type"],
        unique=False,
    )
    op.create_index(
        op.f("ix_learning_tasks_trigger_id"),
        "learning_tasks",
        ["trigger_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_learning_tasks_user_id"),
        "learning_tasks",
        ["user_id"],
        unique=False,
    )

    # Preserve already queued note work from pre-M1 installations. The legacy
    # table remains for downgrade safety but is no longer written by the app.
    connection = op.get_bind()
    legacy = sa.inspect(connection).get_table_names()
    if "note_generation_tasks" in legacy:
        op.execute(
            """
            INSERT INTO learning_tasks (
                id, learning_run_id, user_id, section_id, task_type,
                idempotency_key, trigger_id, payload_json, result_json,
                status, attempt_count, max_attempts, error_code,
                error_message, created_at, updated_at
            )
            SELECT
                id, learning_run_id, user_id, section_id, 'note_generation',
                'legacy:' || id, trigger_attempt_id, '{}', '{}',
                status, attempt_count, 3, error_code, error_message,
                created_at, updated_at
            FROM note_generation_tasks
            """
        )
        op.drop_table("note_generation_tasks")


def downgrade():
    op.create_table(
        "note_generation_tasks",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("learning_run_id", sa.String(), nullable=False),
        sa.Column("section_id", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("trigger_attempt_id", sa.String(), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("error_code", sa.String(length=80), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["learning_run_id"], ["learning_runs.id"]),
        sa.ForeignKeyConstraint(["section_id"], ["sections.id"]),
        sa.ForeignKeyConstraint(["trigger_attempt_id"], ["quiz_attempts.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("trigger_attempt_id"),
    )
    op.create_index(
        "ix_note_generation_tasks_learning_run_id",
        "note_generation_tasks",
        ["learning_run_id"],
        unique=False,
    )
    op.create_index(
        "ix_note_generation_tasks_section_id",
        "note_generation_tasks",
        ["section_id"],
        unique=False,
    )
    op.create_index(
        "ix_note_generation_tasks_status",
        "note_generation_tasks",
        ["status"],
        unique=False,
    )
    op.create_index(
        "ix_note_generation_tasks_user_id",
        "note_generation_tasks",
        ["user_id"],
        unique=False,
    )
    op.execute(
        """
        INSERT INTO note_generation_tasks (
            id, learning_run_id, section_id, user_id, trigger_attempt_id,
            status, attempt_count, error_code, error_message,
            created_at, updated_at
        )
        SELECT
            id, learning_run_id, section_id, user_id, trigger_id,
            status, attempt_count, error_code, error_message,
            created_at, updated_at
        FROM learning_tasks
        WHERE task_type = 'note_generation'
        """
    )
    op.drop_index(op.f("ix_learning_tasks_user_id"), table_name="learning_tasks")
    op.drop_index(op.f("ix_learning_tasks_trigger_id"), table_name="learning_tasks")
    op.drop_index(op.f("ix_learning_tasks_task_type"), table_name="learning_tasks")
    op.drop_index(op.f("ix_learning_tasks_status"), table_name="learning_tasks")
    op.drop_index(op.f("ix_learning_tasks_section_id"), table_name="learning_tasks")
    op.drop_index(
        op.f("ix_learning_tasks_learning_run_id"),
        table_name="learning_tasks",
    )
    op.drop_table("learning_tasks")
