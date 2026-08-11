"""Add adaptive lesson-composition metadata to immutable content blocks.

Revision ID: 0050_adaptive_lesson_composition
Revises: 0049_alpha_account_recovery
"""

from alembic import op
import sqlalchemy as sa


revision = "0050_adaptive_lesson_composition"
down_revision = "0049_alpha_account_recovery"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {
        item["name"]
        for item in inspector.get_columns("content_block_versions")
    }
    additions = (
        ("teaching_moves_json", sa.Text(), "'[]'"),
        ("case_kind", sa.String(32), "''"),
        ("relation_to_anchor", sa.String(32), "''"),
        ("reader_priority", sa.String(16), "'normal'"),
    )
    for name, type_, default in additions:
        if name not in columns:
            op.add_column(
                "content_block_versions",
                sa.Column(name, type_, nullable=False, server_default=sa.text(default)),
            )
    inspector = sa.inspect(bind)
    indexes = {
        item["name"]
        for item in inspector.get_indexes("content_block_versions")
    }
    if "ix_content_block_versions_case_kind" not in indexes:
        op.create_index(
            "ix_content_block_versions_case_kind",
            "content_block_versions",
            ["case_kind"],
            unique=False,
        )


def downgrade():
    op.drop_index(
        "ix_content_block_versions_case_kind",
        table_name="content_block_versions",
    )
    with op.batch_alter_table("content_block_versions") as batch:
        batch.drop_column("reader_priority")
        batch.drop_column("relation_to_anchor")
        batch.drop_column("case_kind")
        batch.drop_column("teaching_moves_json")
