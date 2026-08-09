"""Add versioned knowledge fact graph publication authority.

Revision ID: 0043_knowledge_fact_graph
Revises: 0042_curriculum_baselines
"""

from alembic import op
import sqlalchemy as sa


revision = "0043_knowledge_fact_graph"
down_revision = "0042_curriculum_baselines"
branch_labels = None
depends_on = None


def upgrade():
    required_tables = {
        "knowledge_graph_releases",
        "knowledge_source_versions",
        "concept_relation_versions",
        "concept_objective_bindings",
        "knowledge_claim_bindings",
    }
    existing_tables = set(sa.inspect(op.get_bind()).get_table_names())
    if required_tables.issubset(existing_tables):
        return
    partial = required_tables & existing_tables
    if partial:
        raise RuntimeError(
            "partial knowledge fact graph schema requires explicit repair: "
            + ", ".join(sorted(partial))
        )

    op.create_table(
        "knowledge_graph_releases",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("baseline_version_id", sa.String(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("manifest_json", sa.Text(), nullable=False),
        sa.Column("gaps_json", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("review_json", sa.Text(), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["baseline_version_id"], ["curriculum_baseline_versions.id"]
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("content_hash"),
        sa.UniqueConstraint(
            "baseline_version_id",
            "version",
            name="uq_knowledge_graph_release_baseline_version",
        ),
    )
    for column in ("baseline_version_id", "status"):
        op.create_index(
            op.f(f"ix_knowledge_graph_releases_{column}"),
            "knowledge_graph_releases",
            [column],
        )

    op.create_table(
        "knowledge_source_versions",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("source_key", sa.String(160), nullable=False),
        sa.Column("source_kind", sa.String(40), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("authority", sa.String(240), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("version_label", sa.String(200), nullable=False),
        sa.Column("retrieval_date", sa.String(32), nullable=False),
        sa.Column("content_digest", sa.String(64), nullable=False),
        sa.Column("rights_status", sa.String(32), nullable=False),
        sa.Column("verification_status", sa.String(32), nullable=False),
        sa.Column("provenance_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "source_key",
            "version_label",
            name="uq_knowledge_source_key_version",
        ),
    )
    for column in (
        "source_key",
        "source_kind",
        "rights_status",
        "verification_status",
    ):
        op.create_index(
            op.f(f"ix_knowledge_source_versions_{column}"),
            "knowledge_source_versions",
            [column],
        )

    op.create_table(
        "concept_relation_versions",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("release_id", sa.String(), nullable=False),
        sa.Column("from_concept_revision_id", sa.String(), nullable=False),
        sa.Column("to_concept_revision_id", sa.String(), nullable=False),
        sa.Column("relation_type", sa.String(40), nullable=False),
        sa.Column("relation_revision", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("provenance_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["from_concept_revision_id"], ["concept_revisions.id"]
        ),
        sa.ForeignKeyConstraint(
            ["release_id"], ["knowledge_graph_releases.id"]
        ),
        sa.ForeignKeyConstraint(
            ["to_concept_revision_id"], ["concept_revisions.id"]
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "release_id",
            "from_concept_revision_id",
            "to_concept_revision_id",
            "relation_type",
            name="uq_concept_relation_release_identity",
        ),
    )
    for column in (
        "release_id",
        "from_concept_revision_id",
        "to_concept_revision_id",
        "relation_type",
        "status",
    ):
        op.create_index(
            op.f(f"ix_concept_relation_versions_{column}"),
            "concept_relation_versions",
            [column],
        )

    op.create_table(
        "concept_objective_bindings",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("release_id", sa.String(), nullable=False),
        sa.Column("concept_revision_id", sa.String(), nullable=False),
        sa.Column("learning_objective_id", sa.String(), nullable=False),
        sa.Column("binding_role", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["concept_revision_id"], ["concept_revisions.id"]
        ),
        sa.ForeignKeyConstraint(
            ["learning_objective_id"], ["learning_objectives.id"]
        ),
        sa.ForeignKeyConstraint(
            ["release_id"], ["knowledge_graph_releases.id"]
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "release_id",
            "concept_revision_id",
            "learning_objective_id",
            name="uq_concept_objective_release_identity",
        ),
    )
    for column in ("release_id", "concept_revision_id", "learning_objective_id"):
        op.create_index(
            op.f(f"ix_concept_objective_bindings_{column}"),
            "concept_objective_bindings",
            [column],
        )

    op.create_table(
        "knowledge_claim_bindings",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("release_id", sa.String(), nullable=False),
        sa.Column("source_claim_version_id", sa.String(), nullable=False),
        sa.Column("knowledge_source_version_id", sa.String(), nullable=False),
        sa.Column("locator_type", sa.String(32), nullable=False),
        sa.Column("locator_json", sa.Text(), nullable=False),
        sa.Column("locator_hash", sa.String(64), nullable=False),
        sa.Column("excerpt_hash", sa.String(64), nullable=False),
        sa.Column("support_type", sa.String(24), nullable=False),
        sa.Column("verification_status", sa.String(32), nullable=False),
        sa.Column("review_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["knowledge_source_version_id"], ["knowledge_source_versions.id"]
        ),
        sa.ForeignKeyConstraint(
            ["release_id"], ["knowledge_graph_releases.id"]
        ),
        sa.ForeignKeyConstraint(
            ["source_claim_version_id"], ["source_claim_versions.id"]
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "release_id",
            "source_claim_version_id",
            "knowledge_source_version_id",
            "locator_hash",
            name="uq_knowledge_claim_binding_identity",
        ),
    )
    for column in (
        "release_id",
        "source_claim_version_id",
        "knowledge_source_version_id",
        "verification_status",
    ):
        op.create_index(
            op.f(f"ix_knowledge_claim_bindings_{column}"),
            "knowledge_claim_bindings",
            [column],
        )


def downgrade():
    op.drop_table("knowledge_claim_bindings")
    op.drop_table("concept_objective_bindings")
    op.drop_table("concept_relation_versions")
    op.drop_table("knowledge_source_versions")
    op.drop_table("knowledge_graph_releases")
