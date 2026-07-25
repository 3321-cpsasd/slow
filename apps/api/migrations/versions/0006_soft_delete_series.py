"""add auditable soft deletion for learning series

Revision ID: 0006_soft_delete_series
Revises: 0005_backfill_content_block_ids
"""

from alembic import op
import sqlalchemy as sa

revision = "0006_soft_delete_series"
down_revision = "0005_backfill_content_block_ids"
branch_labels = None
depends_on = None


def upgrade():
    connection = op.get_bind()
    columns = {item["name"] for item in sa.inspect(connection).get_columns("series")}
    if "deleted_at" not in columns:
        op.add_column("series", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))
        op.create_index("ix_series_deleted_at", "series", ["deleted_at"], unique=False)


def downgrade():
    connection = op.get_bind()
    columns = {item["name"] for item in sa.inspect(connection).get_columns("series")}
    if "deleted_at" in columns:
        indexes = {item["name"] for item in sa.inspect(connection).get_indexes("series")}
        if "ix_series_deleted_at" in indexes:
            op.drop_index("ix_series_deleted_at", table_name="series")
        op.drop_column("series", "deleted_at")
