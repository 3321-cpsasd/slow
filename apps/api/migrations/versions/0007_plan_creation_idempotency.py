"""add idempotent plan creation requests

Revision ID: 0007_plan_creation_idempotency
Revises: 0006_soft_delete_series
"""

from alembic import op
import sqlalchemy as sa

revision = "0007_plan_creation_idempotency"
down_revision = "0006_soft_delete_series"
branch_labels = None
depends_on = None


def upgrade():
    connection = op.get_bind()
    if "plan_creation_requests" not in sa.inspect(connection).get_table_names():
        op.create_table(
            "plan_creation_requests",
            sa.Column("idempotency_key", sa.String(length=128), nullable=False),
            sa.Column("user_id", sa.String(), nullable=False),
            sa.Column("request_hash", sa.String(length=64), nullable=False),
            sa.Column("status", sa.String(length=24), nullable=False),
            sa.Column("series_id", sa.String(), nullable=True),
            sa.Column("error_code", sa.String(length=80), nullable=False, server_default=""),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["series_id"], ["series.id"]),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
            sa.PrimaryKeyConstraint("idempotency_key"),
        )
        op.create_index("ix_plan_creation_requests_status", "plan_creation_requests", ["status"], unique=False)
        op.create_index("ix_plan_creation_requests_user_id", "plan_creation_requests", ["user_id"], unique=False)


def downgrade():
    connection = op.get_bind()
    if "plan_creation_requests" in sa.inspect(connection).get_table_names():
        op.drop_index("ix_plan_creation_requests_user_id", table_name="plan_creation_requests")
        op.drop_index("ix_plan_creation_requests_status", table_name="plan_creation_requests")
        op.drop_table("plan_creation_requests")
