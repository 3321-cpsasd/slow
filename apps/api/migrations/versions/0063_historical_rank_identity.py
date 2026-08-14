"""Add append-only historical rank identity decisions.

Revision ID: 0063_historical_rank_identity
Revises: 0062_learning_start_choices
"""

from alembic import op
import sqlalchemy as sa


revision = "0063_historical_rank_identity"
down_revision = "0062_learning_start_choices"
branch_labels = None
depends_on = None


def upgrade():
    table_name = "assessment_target_rank_identity_decisions"
    if table_name in set(sa.inspect(op.get_bind()).get_table_names()):
        return
    op.create_table(
        table_name,
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("source_contract_version_id", sa.String(), nullable=False),
        sa.Column("source_assessment_target_id", sa.String(), nullable=False),
        sa.Column("destination_assessment_target_id", sa.String(), nullable=True),
        sa.Column("decision", sa.String(length=24), nullable=False),
        sa.Column("supersedes_id", sa.String(), nullable=True),
        sa.Column("basis_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("rule_version", sa.String(length=48), nullable=False),
        sa.Column("decision_hash", sa.String(length=64), nullable=False),
        sa.Column("actor_kind", sa.String(length=32), nullable=False),
        sa.Column("actor_id", sa.String(length=160), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["source_contract_version_id"], ["learning_contract_versions.id"]
        ),
        sa.ForeignKeyConstraint(
            ["source_assessment_target_id"], ["assessment_targets.id"]
        ),
        sa.ForeignKeyConstraint(
            ["destination_assessment_target_id"], ["assessment_targets.id"]
        ),
        sa.ForeignKeyConstraint(
            ["supersedes_id"], [f"{table_name}.id"]
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "decision_hash", name="uq_rank_identity_decision_hash"
        ),
    )
    for column in (
        "source_contract_version_id",
        "source_assessment_target_id",
        "destination_assessment_target_id",
        "decision",
        "supersedes_id",
        "rule_version",
        "created_at",
    ):
        op.create_index(
            f"ix_rank_identity_decisions_{column}", table_name, [column]
        )


def downgrade():
    if "assessment_target_rank_identity_decisions" in set(
        sa.inspect(op.get_bind()).get_table_names()
    ):
        op.drop_table("assessment_target_rank_identity_decisions")
