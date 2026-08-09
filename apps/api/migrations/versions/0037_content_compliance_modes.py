"""Add independent generation, rights, factual, and AI-label states.

Revision ID: 0037_content_compliance_modes
Revises: 0036_learning_preferences
"""

from alembic import op
import sqlalchemy as sa


revision = "0037_content_compliance_modes"
down_revision = "0036_learning_preferences"
branch_labels = None
depends_on = None


def _columns():
    return {
        item["name"]
        for item in sa.inspect(op.get_bind()).get_columns("content_versions")
    }


def upgrade():
    existing = _columns()
    additions = [
        ("generation_mode", sa.String(32), "model_only"),
        ("rights_status", sa.String(32), "not_applicable"),
        ("factual_status", sa.String(32), "unreviewed"),
        ("ai_generated", sa.Boolean(), sa.true()),
        ("output_hash", sa.String(64), ""),
        ("labeling_metadata_json", sa.Text(), "{}"),
    ]
    for name, kind, default in additions:
        if name not in existing:
            op.add_column(
                "content_versions",
                sa.Column(name, kind, nullable=False, server_default=default),
            )
    if "generation_run_id" not in existing:
        op.add_column(
            "content_versions",
            sa.Column("generation_run_id", sa.String(), nullable=True),
        )
        with op.batch_alter_table("content_versions") as batch_op:
            batch_op.create_foreign_key(
                "fk_content_versions_generation_run_id",
                "generation_runs",
                ["generation_run_id"],
                ["id"],
            )

    for column in (
        "generation_mode",
        "rights_status",
        "factual_status",
        "generation_run_id",
    ):
        index = f"ix_content_versions_{column}"
        indexes = {
            item["name"]
            for item in sa.inspect(op.get_bind()).get_indexes("content_versions")
        }
        if index not in indexes:
            op.create_index(index, "content_versions", [column])


def downgrade():
    existing = _columns()
    indexes = {
        item["name"]
        for item in sa.inspect(op.get_bind()).get_indexes("content_versions")
    }
    for column in (
        "generation_mode",
        "rights_status",
        "factual_status",
        "generation_run_id",
    ):
        index = f"ix_content_versions_{column}"
        if index in indexes:
            op.drop_index(index, table_name="content_versions")
    if "generation_run_id" in existing:
        with op.batch_alter_table("content_versions") as batch_op:
            batch_op.drop_constraint(
                "fk_content_versions_generation_run_id",
                type_="foreignkey",
            )
            batch_op.drop_column("generation_run_id")
    for column in (
        "labeling_metadata_json",
        "output_hash",
        "ai_generated",
        "factual_status",
        "rights_status",
        "generation_mode",
    ):
        if column in existing:
            op.drop_column("content_versions", column)
