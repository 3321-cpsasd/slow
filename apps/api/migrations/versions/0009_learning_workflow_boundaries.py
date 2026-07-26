"""Add quiz idempotency and durable note-generation tasks.

Revision ID: 0009_learning_workflow_boundaries
Revises: 0008_soft_delete_books
"""

from alembic import op
import sqlalchemy as sa


revision = "0009_learning_workflow_boundaries"
down_revision = "0008_soft_delete_books"
branch_labels = None
depends_on = None


NAMING_CONVENTION = {
    "uq": "uq_%(table_name)s_%(column_0_name)s",
}


def _columns(inspector, table_name):
    return {item["name"] for item in inspector.get_columns(table_name)}


def upgrade():
    connection = op.get_bind()
    inspector = sa.inspect(connection)
    quiz_columns = _columns(inspector, "quiz_attempts")
    if "idempotency_key" not in quiz_columns:
        op.add_column(
            "quiz_attempts",
            sa.Column("idempotency_key", sa.String(length=128), nullable=True),
        )
    if "request_hash" not in quiz_columns:
        op.add_column(
            "quiz_attempts",
            sa.Column(
                "request_hash",
                sa.String(length=64),
                nullable=False,
                server_default="",
            ),
        )
    inspector = sa.inspect(connection)
    quiz_uniques = {
        tuple(item["column_names"])
        for item in inspector.get_unique_constraints("quiz_attempts")
    }
    if ("user_id", "idempotency_key") not in quiz_uniques:
        with op.batch_alter_table(
            "quiz_attempts",
            naming_convention=NAMING_CONVENTION,
        ) as batch_op:
            batch_op.create_unique_constraint(
                "uq_quiz_attempts_user_id_idempotency_key",
                ["user_id", "idempotency_key"],
            )

    inspector = sa.inspect(connection)
    if "note_generation_tasks" not in inspector.get_table_names():
        op.create_table(
            "note_generation_tasks",
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("section_id", sa.String(), nullable=False),
            sa.Column("user_id", sa.String(), nullable=False),
            sa.Column("trigger_attempt_id", sa.String(), nullable=False),
            sa.Column(
                "status",
                sa.String(length=24),
                nullable=False,
                server_default="pending",
            ),
            sa.Column(
                "attempt_count",
                sa.Integer(),
                nullable=False,
                server_default="0",
            ),
            sa.Column(
                "error_code",
                sa.String(length=80),
                nullable=False,
                server_default="",
            ),
            sa.Column(
                "error_message",
                sa.Text(),
                nullable=False,
                server_default="",
            ),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["section_id"], ["sections.id"]),
            sa.ForeignKeyConstraint(["trigger_attempt_id"], ["quiz_attempts.id"]),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("trigger_attempt_id"),
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

    # Old schemas made section_id globally unique. Rebuild these two tables so
    # the authority is correctly scoped to (section, user).
    inspector = sa.inspect(connection)
    qa_uniques = {
        tuple(item["column_names"])
        for item in inspector.get_unique_constraints("qa_sessions")
    }
    if ("section_id",) in qa_uniques and ("section_id", "user_id") not in qa_uniques:
        with op.batch_alter_table(
            "qa_sessions",
            recreate="always",
            naming_convention=NAMING_CONVENTION,
        ) as batch_op:
            batch_op.drop_constraint(
                "uq_qa_sessions_section_id",
                type_="unique",
            )
            batch_op.create_unique_constraint(
                "uq_qa_sessions_section_id_user_id",
                ["section_id", "user_id"],
            )

    inspector = sa.inspect(connection)
    note_uniques = {
        tuple(item["column_names"])
        for item in inspector.get_unique_constraints("learning_notes")
    }
    if ("section_id",) in note_uniques and ("section_id", "user_id") not in note_uniques:
        with op.batch_alter_table(
            "learning_notes",
            recreate="always",
            naming_convention=NAMING_CONVENTION,
        ) as batch_op:
            batch_op.drop_constraint(
                "uq_learning_notes_section_id",
                type_="unique",
            )
            batch_op.create_unique_constraint(
                "uq_learning_notes_section_id_user_id",
                ["section_id", "user_id"],
            )


def downgrade():
    connection = op.get_bind()
    inspector = sa.inspect(connection)
    if "note_generation_tasks" in inspector.get_table_names():
        op.drop_index(
            "ix_note_generation_tasks_user_id",
            table_name="note_generation_tasks",
        )
        op.drop_index(
            "ix_note_generation_tasks_status",
            table_name="note_generation_tasks",
        )
        op.drop_index(
            "ix_note_generation_tasks_section_id",
            table_name="note_generation_tasks",
        )
        op.drop_table("note_generation_tasks")
    quiz_columns = _columns(sa.inspect(connection), "quiz_attempts")
    with op.batch_alter_table("quiz_attempts") as batch_op:
        batch_op.drop_constraint(
            "uq_quiz_attempts_user_id_idempotency_key",
            type_="unique",
        )
        if "request_hash" in quiz_columns:
            batch_op.drop_column("request_hash")
        if "idempotency_key" in quiz_columns:
            batch_op.drop_column("idempotency_key")
