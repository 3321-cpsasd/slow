"""Enforce learning-run references on all run-scoped facts.

Revision ID: 0013_learning_run_fks
Revises: 0012_content_lineage
"""

from alembic import op
import sqlalchemy as sa


revision = "0013_learning_run_fks"
down_revision = "0012_content_lineage"
branch_labels = None
depends_on = None

RUN_SCOPED_TABLES = [
    "quiz_attempts",
    "qa_sessions",
    "learning_notes",
    "note_generation_tasks",
    "learning_evidence",
    "ask_me_sessions",
    "artifact_attachments",
]


def _has_run_foreign_key(connection, table_name):
    return any(
        tuple(item["constrained_columns"]) == ("learning_run_id",)
        and item["referred_table"] == "learning_runs"
        for item in sa.inspect(connection).get_foreign_keys(table_name)
    )


def upgrade():
    connection = op.get_bind()
    for table_name in RUN_SCOPED_TABLES:
        if _has_run_foreign_key(connection, table_name):
            continue
        with op.batch_alter_table(
            table_name,
            recreate="always",
        ) as batch_op:
            batch_op.create_foreign_key(
                f"fk_{table_name}_learning_run_id",
                "learning_runs",
                ["learning_run_id"],
                ["id"],
            )


def downgrade():
    connection = op.get_bind()
    for table_name in reversed(RUN_SCOPED_TABLES):
        if not _has_run_foreign_key(connection, table_name):
            continue
        with op.batch_alter_table(
            table_name,
            recreate="always",
        ) as batch_op:
            batch_op.drop_constraint(
                f"fk_{table_name}_learning_run_id",
                type_="foreignkey",
            )
