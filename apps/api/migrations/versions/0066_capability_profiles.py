"""Add cumulative capability stages and three-axis learner projections.

Revision ID: 0066_capability_profiles
Revises: 0065_knowledge_identity_resolution
"""

from alembic import op
import sqlalchemy as sa


revision = "0066_capability_profiles"
down_revision = "0065_knowledge_identity_resolution"
branch_labels = None
depends_on = None


def _index(table: str, column: str) -> None:
    op.create_index(f"ix_{table}_{column}", table, [column])


def upgrade():
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "capabilities" not in tables:
        op.create_table(
            "capabilities",
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("namespace", sa.String(length=120), nullable=False),
            sa.Column("capability_key", sa.String(length=200), nullable=False),
            sa.Column("canonical_name", sa.String(length=300), nullable=False),
            sa.Column("status", sa.String(length=24), nullable=False),
            sa.Column("origin", sa.String(length=40), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "namespace", "capability_key", name="uq_capabilities_namespace_key"
            ),
        )
        _index("capabilities", "namespace")
        _index("capabilities", "status")

    if "capability_revisions" not in tables:
        op.create_table(
            "capability_revisions",
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("capability_id", sa.String(), nullable=False),
            sa.Column("revision", sa.Integer(), nullable=False),
            sa.Column("label", sa.String(length=300), nullable=False),
            sa.Column("scope_json", sa.Text(), nullable=False),
            sa.Column("operation_json", sa.Text(), nullable=False),
            sa.Column("context_constraints_json", sa.Text(), nullable=False),
            sa.Column("natural_stage_ceiling", sa.String(length=24), nullable=False),
            sa.Column("provenance_mode", sa.String(length=40), nullable=False),
            sa.Column("verification_status", sa.String(length=32), nullable=False),
            sa.Column("supersedes_id", sa.String(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["capability_id"], ["capabilities.id"]),
            sa.ForeignKeyConstraint(["supersedes_id"], ["capability_revisions.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "capability_id",
                "revision",
                name="uq_capability_revisions_capability_revision",
            ),
        )
        _index("capability_revisions", "capability_id")
        _index("capability_revisions", "natural_stage_ceiling")
        _index("capability_revisions", "verification_status")

    if "capability_concept_bindings" not in tables:
        op.create_table(
            "capability_concept_bindings",
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("capability_revision_id", sa.String(), nullable=False),
            sa.Column("concept_revision_id", sa.String(), nullable=False),
            sa.Column("role", sa.String(length=24), nullable=False),
            sa.Column("position", sa.Integer(), nullable=False),
            sa.Column("required", sa.Boolean(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(
                ["capability_revision_id"], ["capability_revisions.id"]
            ),
            sa.ForeignKeyConstraint(
                ["concept_revision_id"], ["concept_revisions.id"]
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "capability_revision_id",
                "concept_revision_id",
                name="uq_capability_concept_binding_identity",
            ),
        )
        _index("capability_concept_bindings", "capability_revision_id")
        _index("capability_concept_bindings", "concept_revision_id")
        _index("capability_concept_bindings", "role")

    if "capability_stage_criteria" not in tables:
        op.create_table(
            "capability_stage_criteria",
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("capability_revision_id", sa.String(), nullable=False),
            sa.Column("stage", sa.String(length=24), nullable=False),
            sa.Column("position", sa.Integer(), nullable=False),
            sa.Column("statement", sa.Text(), nullable=False),
            sa.Column("task_type", sa.String(length=40), nullable=False),
            sa.Column("novelty_requirement", sa.String(length=32), nullable=False),
            sa.Column("assistance_limit", sa.String(length=32), nullable=False),
            sa.Column("context_requirement", sa.String(length=40), nullable=False),
            sa.Column("required", sa.Boolean(), nullable=False),
            sa.Column("verification_protocol", sa.String(length=48), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(
                ["capability_revision_id"], ["capability_revisions.id"]
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "capability_revision_id",
                "stage",
                "position",
                name="uq_capability_stage_criterion_position",
            ),
        )
        _index("capability_stage_criteria", "capability_revision_id")
        _index("capability_stage_criteria", "stage")
        _index("capability_stage_criteria", "task_type")
        _index("capability_stage_criteria", "verification_protocol")

    if "capability_route_bindings" not in tables:
        op.create_table(
            "capability_route_bindings",
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("series_id", sa.String(), nullable=False),
            sa.Column("capability_revision_id", sa.String(), nullable=False),
            sa.Column("target_stage", sa.String(length=24), nullable=False),
            sa.Column("route_json", sa.Text(), nullable=False),
            sa.Column("opportunities_json", sa.Text(), nullable=False),
            sa.Column("status", sa.String(length=24), nullable=False),
            sa.Column("rule_version", sa.String(length=48), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["series_id"], ["series.id"]),
            sa.ForeignKeyConstraint(
                ["capability_revision_id"], ["capability_revisions.id"]
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "series_id",
                "capability_revision_id",
                name="uq_capability_route_binding_identity",
            ),
        )
        _index("capability_route_bindings", "series_id")
        _index("capability_route_bindings", "capability_revision_id")
        _index("capability_route_bindings", "target_stage")
        _index("capability_route_bindings", "status")

    assessment_columns = {
        item["name"]
        for item in sa.inspect(op.get_bind()).get_columns("assessment_targets")
    }
    with op.batch_alter_table("assessment_targets") as batch:
        if "capability_revision_id" not in assessment_columns:
            batch.add_column(sa.Column("capability_revision_id", sa.String(), nullable=True))
            batch.create_foreign_key(
                "fk_assessment_targets_capability_revision",
                "capability_revisions",
                ["capability_revision_id"],
                ["id"],
            )
            batch.create_index(
                "ix_assessment_targets_capability_revision_id",
                ["capability_revision_id"],
            )
        if "capability_stage_criterion_id" not in assessment_columns:
            batch.add_column(
                sa.Column("capability_stage_criterion_id", sa.String(), nullable=True)
            )
            batch.create_foreign_key(
                "fk_assessment_targets_capability_stage_criterion",
                "capability_stage_criteria",
                ["capability_stage_criterion_id"],
                ["id"],
            )
            batch.create_index(
                "ix_assessment_targets_capability_stage_criterion_id",
                ["capability_stage_criterion_id"],
            )

    if "capability_state_projections" not in tables:
        op.create_table(
            "capability_state_projections",
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("user_id", sa.String(), nullable=False),
            sa.Column("capability_revision_id", sa.String(), nullable=False),
            sa.Column("current_stage", sa.String(length=24), nullable=False),
            sa.Column("current_stage_order", sa.Integer(), nullable=False),
            sa.Column("highest_stage", sa.String(length=24), nullable=False),
            sa.Column("highest_stage_order", sa.Integer(), nullable=False),
            sa.Column("satisfied_criterion_ids_json", sa.Text(), nullable=False),
            sa.Column("missing_criterion_ids_json", sa.Text(), nullable=False),
            sa.Column("evidence_maturity_json", sa.Text(), nullable=False),
            sa.Column("activation_state", sa.String(length=24), nullable=False),
            sa.Column("stability_days", sa.Integer(), nullable=False),
            sa.Column("next_due_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_qualified_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("evidence_count", sa.Integer(), nullable=False),
            sa.Column("independent_evidence_count", sa.Integer(), nullable=False),
            sa.Column("projection_rule_version", sa.String(length=48), nullable=False),
            sa.Column("projection_version", sa.Integer(), nullable=False),
            sa.Column("source_observation_watermark", sa.Integer(), nullable=False),
            sa.Column("rebuilt_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(
                ["capability_revision_id"], ["capability_revisions.id"]
            ),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "user_id",
                "capability_revision_id",
                name="uq_capability_state_user_revision",
            ),
        )
        _index("capability_state_projections", "user_id")
        _index("capability_state_projections", "capability_revision_id")
        _index("capability_state_projections", "current_stage")
        _index("capability_state_projections", "highest_stage")
        _index("capability_state_projections", "activation_state")
        _index("capability_state_projections", "next_due_at")


def downgrade():
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "capability_state_projections" in tables:
        op.drop_table("capability_state_projections")
    columns = {
        item["name"]
        for item in sa.inspect(op.get_bind()).get_columns("assessment_targets")
    }
    with op.batch_alter_table("assessment_targets") as batch:
        if "capability_stage_criterion_id" in columns:
            batch.drop_index("ix_assessment_targets_capability_stage_criterion_id")
            batch.drop_constraint(
                "fk_assessment_targets_capability_stage_criterion",
                type_="foreignkey",
            )
            batch.drop_column("capability_stage_criterion_id")
        if "capability_revision_id" in columns:
            batch.drop_index("ix_assessment_targets_capability_revision_id")
            batch.drop_constraint(
                "fk_assessment_targets_capability_revision", type_="foreignkey"
            )
            batch.drop_column("capability_revision_id")
    for table in (
        "capability_route_bindings",
        "capability_stage_criteria",
        "capability_concept_bindings",
        "capability_revisions",
        "capabilities",
    ):
        if table in tables:
            op.drop_table(table)
