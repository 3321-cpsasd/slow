"""Add immutable assessment answer authority.

Revision ID: 0060_trusted_assessment_answers
Revises: 0059_ai_gateway_lineage
"""

import sqlalchemy as sa
from alembic import op


revision = "0060_trusted_assessment_answers"
down_revision = "0059_ai_gateway_lineage"
branch_labels = None
depends_on = None


def upgrade():
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "assessment_answer_versions" not in tables:
        op.create_table(
            "assessment_answer_versions",
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("assessment_item_version_id", sa.String(), nullable=False),
            sa.Column("authority_kind", sa.String(length=48), nullable=False),
            sa.Column("correct_option_ids_json", sa.Text(), nullable=False),
            sa.Column("option_verdicts_json", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("explanation_payload_json", sa.Text(), nullable=False),
            sa.Column("schema_version", sa.String(length=48), nullable=False),
            sa.Column("rule_version", sa.String(length=48), nullable=False),
            sa.Column("verdict_hash", sa.String(length=64), nullable=False),
            sa.Column("publication_status", sa.String(length=24), nullable=False, server_default="published"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["assessment_item_version_id"], ["assessment_item_versions.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("assessment_item_version_id", name="uq_assessment_answer_item_version"),
            sa.UniqueConstraint("verdict_hash"),
        )
        op.create_index(
            "ix_assessment_answer_versions_assessment_item_version_id",
            "assessment_answer_versions",
            ["assessment_item_version_id"],
        )
        op.create_index(
            "ix_assessment_answer_versions_authority_kind",
            "assessment_answer_versions",
            ["authority_kind"],
        )
        op.create_index(
            "ix_assessment_answer_versions_publication_status",
            "assessment_answer_versions",
            ["publication_status"],
        )


def downgrade():
    if "assessment_answer_versions" in set(sa.inspect(op.get_bind()).get_table_names()):
        op.drop_table("assessment_answer_versions")
