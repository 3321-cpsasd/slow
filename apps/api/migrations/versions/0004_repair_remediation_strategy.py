"""repair remediation strategy on databases created before the final v0.2 schema

Revision ID: 0004_repair_remediation_strategy
Revises: 0003_artifact_attachments
"""

from alembic import op
import sqlalchemy as sa

revision = "0004_repair_remediation_strategy"
down_revision = "0003_artifact_attachments"
branch_labels = None
depends_on = None


def upgrade():
    connection = op.get_bind()
    columns = {item["name"] for item in sa.inspect(connection).get_columns("remediations")}
    if "strategy" not in columns:
        op.add_column(
            "remediations",
            sa.Column("strategy", sa.String(length=40), nullable=False, server_default="paragraph_locator"),
        )


def downgrade():
    connection = op.get_bind()
    columns = {item["name"] for item in sa.inspect(connection).get_columns("remediations")}
    if "strategy" in columns:
        op.drop_column("remediations", "strategy")
