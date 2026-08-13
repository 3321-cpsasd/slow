"""Persist Ask Me probe lineage for independent evaluation.

Revision ID: 0061_ask_me_role_lineage
Revises: 0060_trusted_assessment_answers
"""

import sqlalchemy as sa
from alembic import op


revision = "0061_ask_me_role_lineage"
down_revision = "0060_trusted_assessment_answers"
branch_labels = None
depends_on = None


def _columns(table_name: str) -> set[str]:
    return {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns(table_name)
    }


def upgrade():
    for table_name in ("ask_me_sessions", "ask_me_discussion_topics"):
        columns = _columns(table_name)
        if "current_probe_deployment_id" not in columns:
            op.add_column(
                table_name,
                sa.Column(
                    "current_probe_deployment_id",
                    sa.String(length=160),
                    nullable=False,
                    server_default="",
                ),
            )
        if "current_probe_model_family_id" not in columns:
            op.add_column(
                table_name,
                sa.Column(
                    "current_probe_model_family_id",
                    sa.String(length=160),
                    nullable=False,
                    server_default="",
                ),
            )


def downgrade():
    for table_name in ("ask_me_discussion_topics", "ask_me_sessions"):
        columns = _columns(table_name)
        if "current_probe_model_family_id" in columns:
            op.drop_column(table_name, "current_probe_model_family_id")
        if "current_probe_deployment_id" in columns:
            op.drop_column(table_name, "current_probe_deployment_id")
