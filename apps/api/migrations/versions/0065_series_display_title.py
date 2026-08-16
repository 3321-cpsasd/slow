"""Add a user-controlled series display title.

Revision ID: 0065_series_display_title
Revises: 0064_shelf_soft_delete
"""

from alembic import op
import sqlalchemy as sa


revision = "0065_series_display_title"
down_revision = "0064_shelf_soft_delete"
branch_labels = None
depends_on = None


def upgrade():
    columns = {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns("series")
    }
    if "display_title" not in columns:
        with op.batch_alter_table("series") as batch_op:
            batch_op.add_column(
                sa.Column("display_title", sa.String(length=240), nullable=True)
            )


def downgrade():
    columns = {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns("series")
    }
    if "display_title" in columns:
        with op.batch_alter_table("series") as batch_op:
            batch_op.drop_column("display_title")
