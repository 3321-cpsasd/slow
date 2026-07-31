"""Allow durable work before the first section exists.

Revision ID: 0015_initial_lesson_preload
Revises: 0014_durable_learning_tasks
"""

from alembic import op
import sqlalchemy as sa


revision = "0015_initial_lesson_preload"
down_revision = "0014_durable_learning_tasks"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("learning_tasks") as batch_op:
        batch_op.alter_column(
            "section_id",
            existing_type=sa.String(),
            nullable=True,
        )


def downgrade():
    op.execute("DELETE FROM learning_tasks WHERE section_id IS NULL")
    with op.batch_alter_table("learning_tasks") as batch_op:
        batch_op.alter_column(
            "section_id",
            existing_type=sa.String(),
            nullable=False,
        )
