"""Add append-only on-demand knowledge identity resolution.

Revision ID: 0065_knowledge_identity_resolution
Revises: 0064_shelf_soft_delete
"""

from alembic import op
import sqlalchemy as sa


revision = "0065_knowledge_identity_resolution"
down_revision = "0064_shelf_soft_delete"
branch_labels = None
depends_on = None


def upgrade():
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "knowledge_identity_candidates" not in tables:
        op.create_table(
            "knowledge_identity_candidates",
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("series_id", sa.String(), nullable=False),
            sa.Column("section_id", sa.String(), nullable=False),
            sa.Column("candidate_key", sa.String(length=160), nullable=False),
            sa.Column("label", sa.String(length=300), nullable=False),
            sa.Column("definition", sa.Text(), nullable=False),
            sa.Column("scope_json", sa.Text(), nullable=False),
            sa.Column("boundaries_json", sa.Text(), nullable=False),
            sa.Column("candidate_hash", sa.String(length=64), nullable=False),
            sa.Column("status", sa.String(length=24), nullable=False),
            sa.Column("provenance_mode", sa.String(length=40), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["section_id"], ["sections.id"]),
            sa.ForeignKeyConstraint(["series_id"], ["series.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "section_id",
                "candidate_hash",
                name="uq_knowledge_identity_candidates_section_hash",
            ),
        )
        op.create_index(
            "ix_knowledge_identity_candidates_series_id",
            "knowledge_identity_candidates",
            ["series_id"],
        )
        op.create_index(
            "ix_knowledge_identity_candidates_section_id",
            "knowledge_identity_candidates",
            ["section_id"],
        )
        op.create_index(
            "ix_knowledge_identity_candidates_candidate_key",
            "knowledge_identity_candidates",
            ["candidate_key"],
        )
        op.create_index(
            "ix_knowledge_identity_candidates_label",
            "knowledge_identity_candidates",
            ["label"],
        )
        op.create_index(
            "ix_knowledge_identity_candidates_candidate_hash",
            "knowledge_identity_candidates",
            ["candidate_hash"],
        )
        op.create_index(
            "ix_knowledge_identity_candidates_status",
            "knowledge_identity_candidates",
            ["status"],
        )

    if "knowledge_identity_decisions" not in tables:
        op.create_table(
            "knowledge_identity_decisions",
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("candidate_id", sa.String(), nullable=False),
            sa.Column("decision", sa.String(length=24), nullable=False),
            sa.Column("resolved_concept_revision_id", sa.String(), nullable=True),
            sa.Column("compared_revision_ids_json", sa.Text(), nullable=False),
            sa.Column("basis_json", sa.Text(), nullable=False),
            sa.Column("actor_kind", sa.String(length=32), nullable=False),
            sa.Column("actor_id", sa.String(length=160), nullable=False),
            sa.Column("rule_version", sa.String(length=48), nullable=False),
            sa.Column("model_version", sa.String(length=80), nullable=False),
            sa.Column("supersedes_id", sa.String(), nullable=True),
            sa.Column("decision_hash", sa.String(length=64), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(
                ["candidate_id"], ["knowledge_identity_candidates.id"]
            ),
            sa.ForeignKeyConstraint(
                ["resolved_concept_revision_id"], ["concept_revisions.id"]
            ),
            sa.ForeignKeyConstraint(
                ["supersedes_id"], ["knowledge_identity_decisions.id"]
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "decision_hash", name="uq_knowledge_identity_decisions_hash"
            ),
        )
        op.create_index(
            "ix_knowledge_identity_decisions_candidate_id",
            "knowledge_identity_decisions",
            ["candidate_id"],
        )
        op.create_index(
            "ix_knowledge_identity_decisions_decision",
            "knowledge_identity_decisions",
            ["decision"],
        )
        op.create_index(
            "ix_knowledge_identity_decisions_resolved_concept_revision_id",
            "knowledge_identity_decisions",
            ["resolved_concept_revision_id"],
        )
        op.create_index(
            "ix_knowledge_identity_decisions_rule_version",
            "knowledge_identity_decisions",
            ["rule_version"],
        )
        op.create_index(
            "ix_knowledge_identity_decisions_supersedes_id",
            "knowledge_identity_decisions",
            ["supersedes_id"],
        )
        op.create_index(
            "ix_knowledge_identity_decisions_created_at",
            "knowledge_identity_decisions",
            ["created_at"],
        )


def downgrade():
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "knowledge_identity_decisions" in tables:
        op.drop_table("knowledge_identity_decisions")
    if "knowledge_identity_candidates" in tables:
        op.drop_table("knowledge_identity_candidates")
