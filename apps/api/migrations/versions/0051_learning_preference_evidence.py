"""Add append-only learning preference evidence.

Revision ID: 0051_learning_preference_evidence
Revises: 0050_adaptive_lesson_composition
"""

from alembic import op
import sqlalchemy as sa


revision = "0051_learning_preference_evidence"
down_revision = "0050_adaptive_lesson_composition"
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    if "learning_preference_evidence" in inspector.get_table_names():
        return
    op.create_table(
        "learning_preference_evidence",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("event_id", sa.String(length=128), nullable=False),
        sa.Column("request_event_id", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("section_id", sa.String(), nullable=False),
        sa.Column("shelf_id", sa.String(), nullable=False),
        sa.Column("content_version_id", sa.String(), nullable=False, server_default=""),
        sa.Column("block_id", sa.String(), nullable=False, server_default=""),
        sa.Column("block_kind", sa.String(length=32), nullable=False, server_default="text"),
        sa.Column("style", sa.String(length=32), nullable=False),
        sa.Column("signal", sa.String(length=24), nullable=False),
        sa.Column("dimensions_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("extraction_confidence", sa.Float(), nullable=False, server_default="1"),
        sa.Column("extractor_version", sa.String(length=32), nullable=False, server_default="preset_v1"),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "event_id", name="uq_learning_preference_evidence_user_event"),
    )
    op.create_index("ix_learning_preference_evidence_user_id", "learning_preference_evidence", ["user_id"])
    op.create_index("ix_learning_preference_evidence_section_id", "learning_preference_evidence", ["section_id"])
    op.create_index("ix_learning_preference_evidence_shelf_id", "learning_preference_evidence", ["shelf_id"])
    op.create_index("ix_learning_preference_evidence_style", "learning_preference_evidence", ["style"])
    op.create_index("ix_learning_preference_evidence_signal", "learning_preference_evidence", ["signal"])
    op.create_index("ix_learning_preference_evidence_occurred_at", "learning_preference_evidence", ["occurred_at"])
    op.create_index(
        "ix_learning_preference_evidence_user_time",
        "learning_preference_evidence",
        ["user_id", "occurred_at"],
    )
    op.create_table(
        "personal_block_presentations",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("section_id", sa.String(), nullable=False),
        sa.Column("content_version_id", sa.String(), nullable=False),
        sa.Column("block_id", sa.String(), nullable=False),
        sa.Column("replacement_content", sa.Text(), nullable=False),
        sa.Column("source_qa_message_id", sa.String(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["section_id"], ["sections.id"]),
        sa.ForeignKeyConstraint(["content_version_id"], ["content_versions.id"]),
        sa.ForeignKeyConstraint(["source_qa_message_id"], ["qa_messages.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "content_version_id", "block_id", name="uq_personal_block_presentations_user_block"),
    )
    for name, columns in (
        ("ix_personal_block_presentations_user_id", ["user_id"]),
        ("ix_personal_block_presentations_section_id", ["section_id"]),
        ("ix_personal_block_presentations_content_version_id", ["content_version_id"]),
        ("ix_personal_block_presentations_block_id", ["block_id"]),
        ("ix_personal_block_presentations_active", ["active"]),
    ):
        op.create_index(name, "personal_block_presentations", columns)


def downgrade():
    op.drop_table("personal_block_presentations")
    op.drop_table("learning_preference_evidence")
