"""Add bounded evidence-guided reinforcement runs.

Revision ID: 0058_reinforcement_agent
Revises: 0057_knowledge_engine_completion
"""

import sqlalchemy as sa
from alembic import op


revision = "0058_reinforcement_agent"
down_revision = "0057_knowledge_engine_completion"
branch_labels = None
depends_on = None


def upgrade():
    expected = {
        "reinforcement_runs",
        "reinforcement_package_versions",
        "reinforcement_activity_versions",
        "reinforcement_events",
    }
    present = expected.intersection(sa.inspect(op.get_bind()).get_table_names())
    # Historical development databases may have been initialized from current
    # ORM metadata before Alembic caught up. Treat a complete matching family as
    # already materialized; a partial family is ambiguous and must fail closed.
    if present == expected:
        return
    if present:
        raise RuntimeError(
            "partial reinforcement schema exists; manual recovery is required"
        )
    op.create_table(
        "reinforcement_runs",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("assessment_target_id", sa.String(), nullable=False),
        sa.Column("source_review_assignment_id", sa.String(), nullable=True),
        sa.Column("source_learning_run_id", sa.String(), nullable=False),
        sa.Column("source_section_id", sa.String(), nullable=False),
        sa.Column("learning_contract_version_id", sa.String(), nullable=False),
        sa.Column("content_version_id", sa.String(), nullable=False),
        sa.Column("entry_mode", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("current_state", sa.String(length=32), nullable=False),
        sa.Column("current_activity_key", sa.String(length=64), nullable=False),
        sa.Column("activity_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("repair_rounds", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_activities", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("max_repair_rounds", sa.Integer(), nullable=False, server_default="2"),
        sa.Column("confirmed_cause_code", sa.String(length=48), nullable=False, server_default=""),
        sa.Column("state_rule_version", sa.String(length=48), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["assessment_target_id"], ["assessment_targets.id"]),
        sa.ForeignKeyConstraint(["source_review_assignment_id"], ["review_assignments.id"]),
        sa.ForeignKeyConstraint(["source_learning_run_id"], ["learning_runs.id"]),
        sa.ForeignKeyConstraint(["source_section_id"], ["sections.id"]),
        sa.ForeignKeyConstraint(["learning_contract_version_id"], ["learning_contract_versions.id"]),
        sa.ForeignKeyConstraint(["content_version_id"], ["content_versions.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source_review_assignment_id", name="uq_reinforcement_runs_review_assignment"),
    )
    for column in ("user_id", "assessment_target_id", "source_review_assignment_id", "source_learning_run_id", "source_section_id", "learning_contract_version_id", "content_version_id", "entry_mode", "status", "current_state"):
        op.create_index(f"ix_reinforcement_runs_{column}", "reinforcement_runs", [column])

    op.create_table(
        "reinforcement_package_versions",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column("generation_run_id", sa.String(), nullable=True),
        sa.Column("verification_quiz_set_id", sa.String(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("schema_version", sa.String(length=48), nullable=False),
        sa.Column("prompt_version", sa.String(length=48), nullable=False),
        sa.Column("input_hash", sa.String(length=64), nullable=False),
        sa.Column("output_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["reinforcement_runs.id"]),
        sa.ForeignKeyConstraint(["generation_run_id"], ["generation_runs.id"]),
        sa.ForeignKeyConstraint(["verification_quiz_set_id"], ["quiz_sets.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "version", name="uq_reinforcement_package_run_version"),
        sa.UniqueConstraint("verification_quiz_set_id"),
    )
    for column in ("run_id", "generation_run_id", "status"):
        op.create_index(f"ix_reinforcement_package_versions_{column}", "reinforcement_package_versions", [column])

    op.create_table(
        "reinforcement_activity_versions",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("package_version_id", sa.String(), nullable=False),
        sa.Column("assessment_target_id", sa.String(), nullable=False),
        sa.Column("activity_key", sa.String(length=64), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("activity_type", sa.String(length=32), nullable=False),
        sa.Column("assistance_mode", sa.String(length=32), nullable=False),
        sa.Column("evidence_role", sa.String(length=32), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("signature", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["package_version_id"], ["reinforcement_package_versions.id"]),
        sa.ForeignKeyConstraint(["assessment_target_id"], ["assessment_targets.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("package_version_id", "activity_key", name="uq_reinforcement_activity_package_key"),
        sa.UniqueConstraint("package_version_id", "position", name="uq_reinforcement_activity_package_position"),
    )
    for column in ("package_version_id", "assessment_target_id", "activity_type"):
        op.create_index(f"ix_reinforcement_activity_versions_{column}", "reinforcement_activity_versions", [column])

    op.create_table(
        "reinforcement_events",
        sa.Column("sequence", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("event_type", sa.String(length=32), nullable=False),
        sa.Column("activity_key", sa.String(length=64), nullable=False),
        sa.Column("state_before", sa.String(length=32), nullable=False),
        sa.Column("state_after", sa.String(length=32), nullable=False),
        sa.Column("response_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("result_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("assistance_mode", sa.String(length=32), nullable=False),
        sa.Column("source_observation_id", sa.String(), nullable=True),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("rule_version", sa.String(length=48), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["reinforcement_runs.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["source_observation_id"], ["assessment_observations.id"]),
        sa.PrimaryKeyConstraint("sequence"),
        sa.UniqueConstraint("id"),
        sa.UniqueConstraint("run_id", "idempotency_key", name="uq_reinforcement_events_run_idempotency"),
    )
    for column in ("id", "run_id", "user_id", "event_type", "source_observation_id"):
        op.create_index(f"ix_reinforcement_events_{column}", "reinforcement_events", [column], unique=column == "id")


def downgrade():
    op.drop_table("reinforcement_events")
    op.drop_table("reinforcement_activity_versions")
    op.drop_table("reinforcement_package_versions")
    op.drop_table("reinforcement_runs")
