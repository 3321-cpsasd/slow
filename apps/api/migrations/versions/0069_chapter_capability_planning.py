"""Add auditable chapter capability and relation planning candidates.

Revision ID: 0069_chapter_capability_planning
Revises: 0068_capability_knowledge_subnets
"""

from alembic import op
import sqlalchemy as sa


revision = "0069_chapter_capability_planning"
down_revision = "0068_capability_knowledge_subnets"
branch_labels = None
depends_on = None


_ALIASES = {
    "knowledge_relation_candidates": "kr_candidate",
    "knowledge_relation_identity_decisions": "kr_decision",
    "capability_planning_candidates": "cap_plan_candidate",
    "capability_planning_decisions": "cap_plan_decision",
}


def _index(table: str, column: str) -> None:
    name = f"ix_{_ALIASES[table]}_{column}"
    if len(name) > 63:
        raise ValueError(f"PostgreSQL index identifier is too long: {name}")
    op.create_index(name, table, [column])


def upgrade():
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "knowledge_relation_candidates" not in tables:
        op.create_table(
            "knowledge_relation_candidates",
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("series_id", sa.String(), nullable=False),
            sa.Column("chapter_id", sa.String(), nullable=False),
            sa.Column("candidate_key", sa.String(length=160), nullable=False),
            sa.Column("from_concept_revision_id", sa.String(), nullable=False),
            sa.Column("to_concept_revision_id", sa.String(), nullable=False),
            sa.Column("relation_type", sa.String(length=40), nullable=False),
            sa.Column("statement", sa.Text(), nullable=False),
            sa.Column("scope_json", sa.Text(), nullable=False),
            sa.Column("candidate_hash", sa.String(length=64), nullable=False),
            sa.Column("status", sa.String(length=24), nullable=False),
            sa.Column("provenance_mode", sa.String(length=40), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["series_id"], ["series.id"]),
            sa.ForeignKeyConstraint(["chapter_id"], ["chapters.id"]),
            sa.ForeignKeyConstraint(
                ["from_concept_revision_id"], ["concept_revisions.id"]
            ),
            sa.ForeignKeyConstraint(
                ["to_concept_revision_id"], ["concept_revisions.id"]
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "chapter_id",
                "candidate_hash",
                name="uq_knowledge_relation_candidate_chapter_hash",
            ),
        )
        for column in (
            "series_id",
            "chapter_id",
            "candidate_key",
            "from_concept_revision_id",
            "to_concept_revision_id",
            "relation_type",
            "candidate_hash",
            "status",
        ):
            _index("knowledge_relation_candidates", column)

    if "knowledge_relation_identity_decisions" not in tables:
        op.create_table(
            "knowledge_relation_identity_decisions",
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("candidate_id", sa.String(), nullable=False),
            sa.Column("decision", sa.String(length=24), nullable=False),
            sa.Column("resolved_relation_revision_id", sa.String(), nullable=True),
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
                ["candidate_id"], ["knowledge_relation_candidates.id"]
            ),
            sa.ForeignKeyConstraint(
                ["resolved_relation_revision_id"], ["knowledge_relation_revisions.id"]
            ),
            sa.ForeignKeyConstraint(
                ["supersedes_id"], ["knowledge_relation_identity_decisions.id"]
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "decision_hash", name="uq_knowledge_relation_decision_hash"
            ),
        )
        for column in (
            "candidate_id",
            "decision",
            "resolved_relation_revision_id",
            "rule_version",
            "supersedes_id",
            "created_at",
        ):
            _index("knowledge_relation_identity_decisions", column)

    if "capability_planning_candidates" not in tables:
        op.create_table(
            "capability_planning_candidates",
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("series_id", sa.String(), nullable=False),
            sa.Column("chapter_id", sa.String(), nullable=False),
            sa.Column("candidate_key", sa.String(length=160), nullable=False),
            sa.Column("label", sa.String(length=300), nullable=False),
            sa.Column("operation", sa.Text(), nullable=False),
            sa.Column("boundary_json", sa.Text(), nullable=False),
            sa.Column("members_json", sa.Text(), nullable=False),
            sa.Column("relations_json", sa.Text(), nullable=False),
            sa.Column("assessment_section_id", sa.String(), nullable=False),
            sa.Column("assessment_objective_position", sa.Integer(), nullable=False),
            sa.Column("natural_stage_ceiling", sa.String(length=24), nullable=False),
            sa.Column("candidate_hash", sa.String(length=64), nullable=False),
            sa.Column("status", sa.String(length=24), nullable=False),
            sa.Column("provenance_mode", sa.String(length=40), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["series_id"], ["series.id"]),
            sa.ForeignKeyConstraint(["chapter_id"], ["chapters.id"]),
            sa.ForeignKeyConstraint(["assessment_section_id"], ["sections.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "chapter_id",
                "candidate_hash",
                name="uq_capability_planning_candidate_chapter_hash",
            ),
        )
        for column in (
            "series_id",
            "chapter_id",
            "candidate_key",
            "assessment_section_id",
            "natural_stage_ceiling",
            "candidate_hash",
            "status",
        ):
            _index("capability_planning_candidates", column)

    if "capability_planning_decisions" not in tables:
        op.create_table(
            "capability_planning_decisions",
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("candidate_id", sa.String(), nullable=False),
            sa.Column("decision", sa.String(length=32), nullable=False),
            sa.Column("resolved_capability_revision_id", sa.String(), nullable=True),
            sa.Column("basis_json", sa.Text(), nullable=False),
            sa.Column("actor_kind", sa.String(length=32), nullable=False),
            sa.Column("actor_id", sa.String(length=160), nullable=False),
            sa.Column("rule_version", sa.String(length=48), nullable=False),
            sa.Column("model_version", sa.String(length=80), nullable=False),
            sa.Column("supersedes_id", sa.String(), nullable=True),
            sa.Column("decision_hash", sa.String(length=64), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(
                ["candidate_id"], ["capability_planning_candidates.id"]
            ),
            sa.ForeignKeyConstraint(
                ["resolved_capability_revision_id"], ["capability_revisions.id"]
            ),
            sa.ForeignKeyConstraint(
                ["supersedes_id"], ["capability_planning_decisions.id"]
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "decision_hash", name="uq_capability_planning_decision_hash"
            ),
        )
        for column in (
            "candidate_id",
            "decision",
            "resolved_capability_revision_id",
            "rule_version",
            "supersedes_id",
            "created_at",
        ):
            _index("capability_planning_decisions", column)


def downgrade():
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    for table in (
        "capability_planning_decisions",
        "capability_planning_candidates",
        "knowledge_relation_identity_decisions",
        "knowledge_relation_candidates",
    ):
        if table in tables:
            op.drop_table(table)
