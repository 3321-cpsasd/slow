"""Add shelf soft-delete authority.

Revision ID: 0064_shelf_soft_delete
Revises: 0063_historical_rank_identity
"""

from alembic import op
import sqlalchemy as sa


revision = "0064_shelf_soft_delete"
down_revision = "0063_historical_rank_identity"
branch_labels = None
depends_on = None


def upgrade():
    columns = {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns("shelves")
    }
    if "deleted_at" not in columns:
        with op.batch_alter_table("shelves") as batch_op:
            batch_op.add_column(
                sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True)
            )
            batch_op.create_index(
                "ix_shelves_deleted_at",
                ["deleted_at"],
                unique=False,
            )


def downgrade():
    columns = {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns("shelves")
    }
    if "deleted_at" in columns:
        with op.batch_alter_table("shelves") as batch_op:
            batch_op.drop_index("ix_shelves_deleted_at")
            batch_op.drop_column("deleted_at")
