"""Add lesson generation v2 publication and binding authority.

Revision ID: 0038_lesson_generation_v2
Revises: 0037_content_compliance_modes
"""

from alembic import op
import sqlalchemy as sa


revision = "0038_lesson_generation_v2"
down_revision = "0037_content_compliance_modes"
branch_labels = None
depends_on = None


def _column_names(table_name: str) -> set[str]:
    return {
        item["name"]
        for item in sa.inspect(op.get_bind()).get_columns(table_name)
    }


def _add_column_if_missing(table_name: str, column: sa.Column) -> None:
    if column.name not in _column_names(table_name):
        op.add_column(table_name, column)


def upgrade():
    _add_column_if_missing(
        "content_versions",
        sa.Column(
            "publication_status",
            sa.String(24),
            nullable=False,
            server_default="published",
        ),
    )
    _add_column_if_missing(
        "content_versions",
        sa.Column(
            "schema_version", sa.String(48), nullable=False, server_default="legacy"
        ),
    )
    _add_column_if_missing(
        "content_versions",
        sa.Column(
            "prompt_version", sa.String(48), nullable=False, server_default="legacy"
        ),
    )
    _add_column_if_missing(
        "quiz_sets",
        sa.Column(
            "publication_status",
            sa.String(24),
            nullable=False,
            server_default="published",
        ),
    )
    _add_column_if_missing(
        "quiz_sets",
        sa.Column(
            "schema_version", sa.String(48), nullable=False, server_default="legacy"
        ),
    )
    for name, length, default in (
        ("pipeline_version", 48, "legacy"),
        ("prompt_version", 48, "legacy"),
        ("schema_version", 48, "legacy"),
        ("generation_mode", 32, "model_only"),
        ("context_hash", 64, ""),
    ):
        _add_column_if_missing(
            "generation_runs",
            sa.Column(name, sa.String(length), nullable=False, server_default=default),
        )

    indexes = {
        item["name"]
        for item in sa.inspect(op.get_bind()).get_indexes("content_versions")
    }
    if "ix_content_versions_publication_status" not in indexes:
        op.create_index(
            "ix_content_versions_publication_status",
            "content_versions",
            ["publication_status"],
        )
    indexes = {
        item["name"]
        for item in sa.inspect(op.get_bind()).get_indexes("quiz_sets")
    }
    if "ix_quiz_sets_publication_status" not in indexes:
        op.create_index(
            "ix_quiz_sets_publication_status",
            "quiz_sets",
            ["publication_status"],
        )

    binding_tables = {
        "content_block_assessment_targets",
        "assessment_item_versions",
        "assessment_item_evidence_blocks",
    }
    existing_tables = set(sa.inspect(op.get_bind()).get_table_names())
    already_present = binding_tables & existing_tables
    if already_present:
        if already_present != binding_tables:
            raise RuntimeError(
                "lesson generation v2 binding tables are only partially present"
            )
        return

    op.create_table(
        "content_block_assessment_targets",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("content_block_version_id", sa.String(), nullable=False),
        sa.Column("assessment_target_id", sa.String(), nullable=False),
        sa.Column(
            "binding_role", sa.String(32), nullable=False, server_default="teaches"
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["content_block_version_id"], ["content_block_versions.id"]
        ),
        sa.ForeignKeyConstraint(["assessment_target_id"], ["assessment_targets.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "content_block_version_id",
            "assessment_target_id",
            name="uq_content_block_assessment_target_identity",
        ),
    )
    op.create_index(
        "ix_content_block_assessment_targets_content_block_version_id",
        "content_block_assessment_targets",
        ["content_block_version_id"],
    )
    op.create_index(
        "ix_content_block_assessment_targets_assessment_target_id",
        "content_block_assessment_targets",
        ["assessment_target_id"],
    )

    op.create_table(
        "assessment_item_versions",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("quiz_set_id", sa.String(), nullable=False),
        sa.Column("assessment_target_id", sa.String(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("item_key", sa.String(64), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["quiz_set_id"], ["quiz_sets.id"]),
        sa.ForeignKeyConstraint(["assessment_target_id"], ["assessment_targets.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "quiz_set_id",
            "position",
            name="uq_assessment_item_versions_quiz_position",
        ),
        sa.UniqueConstraint(
            "quiz_set_id",
            "item_key",
            name="uq_assessment_item_versions_quiz_key",
        ),
    )
    op.create_index(
        "ix_assessment_item_versions_quiz_set_id",
        "assessment_item_versions",
        ["quiz_set_id"],
    )
    op.create_index(
        "ix_assessment_item_versions_assessment_target_id",
        "assessment_item_versions",
        ["assessment_target_id"],
    )

    op.create_table(
        "assessment_item_evidence_blocks",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("assessment_item_version_id", sa.String(), nullable=False),
        sa.Column("content_block_version_id", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["assessment_item_version_id"], ["assessment_item_versions.id"]
        ),
        sa.ForeignKeyConstraint(
            ["content_block_version_id"], ["content_block_versions.id"]
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "assessment_item_version_id",
            "content_block_version_id",
            name="uq_assessment_item_evidence_block_identity",
        ),
    )
    op.create_index(
        "ix_assessment_item_evidence_blocks_assessment_item_version_id",
        "assessment_item_evidence_blocks",
        ["assessment_item_version_id"],
    )
    op.create_index(
        "ix_assessment_item_evidence_blocks_content_block_version_id",
        "assessment_item_evidence_blocks",
        ["content_block_version_id"],
    )


def downgrade():
    op.drop_table("assessment_item_evidence_blocks")
    op.drop_table("assessment_item_versions")
    op.drop_table("content_block_assessment_targets")
    op.drop_index(
        "ix_quiz_sets_publication_status", table_name="quiz_sets"
    )
    op.drop_index(
        "ix_content_versions_publication_status", table_name="content_versions"
    )
    for name in (
        "context_hash",
        "generation_mode",
        "schema_version",
        "prompt_version",
        "pipeline_version",
    ):
        op.drop_column("generation_runs", name)
    op.drop_column("quiz_sets", "schema_version")
    op.drop_column("quiz_sets", "publication_status")
    op.drop_column("content_versions", "prompt_version")
    op.drop_column("content_versions", "schema_version")
    op.drop_column("content_versions", "publication_status")
