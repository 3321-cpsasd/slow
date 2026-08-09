"""Add immutable global and content-block feedback facts.

Revision ID: 0035_user_feedback
Revises: 0034_learning_decision_snapshots
"""

from alembic import op
import sqlalchemy as sa


revision = "0035_user_feedback"
down_revision = "0034_learning_decision_snapshots"
branch_labels = None
depends_on = None


def upgrade():
    if "user_feedback" in set(sa.inspect(op.get_bind()).get_table_names()):
        return
    op.create_table(
        "user_feedback",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("scope", sa.String(length=24), nullable=False),
        sa.Column("feedback_type", sa.String(length=32), nullable=False),
        sa.Column("message", sa.Text(), nullable=False, server_default=""),
        sa.Column("page_path", sa.String(length=500), nullable=False, server_default="/"),
        sa.Column("view", sa.String(length=40), nullable=False, server_default=""),
        sa.Column("section_id", sa.String(), nullable=True),
        sa.Column("content_version_id", sa.String(), nullable=True),
        sa.Column("block_id", sa.String(length=160), nullable=True),
        sa.Column("block_snapshot_hash", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("source_mode", sa.String(length=40), nullable=False),
        sa.Column("schema_version", sa.String(length=40), nullable=False, server_default="feedback_v1"),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("context_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["section_id"], ["sections.id"]),
        sa.ForeignKeyConstraint(["content_version_id"], ["content_versions.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id",
            "idempotency_key",
            name="uq_user_feedback_user_idempotency",
        ),
    )
    for column in (
        "user_id", "scope", "feedback_type", "section_id",
        "content_version_id", "block_id", "created_at",
    ):
        op.create_index(f"ix_user_feedback_{column}", "user_feedback", [column])


def downgrade():
    if "user_feedback" in set(sa.inspect(op.get_bind()).get_table_names()):
        op.drop_table("user_feedback")
