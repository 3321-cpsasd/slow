"""Persist stable candidate-local lesson case identity.

Revision ID: 0053_lesson_case_identity
Revises: 0052_auth_qa_preference_integrity
"""

from alembic import op
import sqlalchemy as sa


revision = "0053_lesson_case_identity"
down_revision = "0052_auth_qa_preference_integrity"
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    columns = {
        item["name"]
        for item in inspector.get_columns("content_block_versions")
    }
    if "case_key" not in columns:
        op.add_column(
            "content_block_versions",
            sa.Column(
                "case_key",
                sa.String(length=64),
                nullable=False,
                server_default="",
            ),
        )


def downgrade():
    op.drop_column("content_block_versions", "case_key")
