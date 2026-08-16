"""Add governed standard-application tasks and immutable evaluation facts.

Revision ID: 0067_capability_application_tasks
Revises: 0066_capability_profiles
"""

from alembic import op
import sqlalchemy as sa


revision = "0067_capability_application_tasks"
down_revision = "0066_capability_profiles"
branch_labels = None
depends_on = None


def _index(table: str, column: str) -> None:
    op.create_index(f"ix_{table}_{column}", table, [column])


def upgrade():
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if {
        "capability_revisions",
        "capability_stage_criteria",
    }.issubset(tables):
        op.execute(sa.text("""
            UPDATE capability_revisions
            SET natural_stage_ceiling = 'gold'
            WHERE natural_stage_ceiling = 'silver'
              AND EXISTS (
                  SELECT 1
                  FROM capability_stage_criteria
                  WHERE capability_stage_criteria.capability_revision_id
                        = capability_revisions.id
                    AND capability_stage_criteria.stage = 'gold'
                    AND capability_stage_criteria.verification_protocol
                        = 'standard_application_v1'
              )
        """))
    if "capability_application_task_versions" not in tables:
        op.create_table(
            "capability_application_task_versions",
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("section_id", sa.String(), nullable=False),
            sa.Column("learning_contract_version_id", sa.String(), nullable=False),
            sa.Column("content_version_id", sa.String(), nullable=False),
            sa.Column("assessment_target_id", sa.String(), nullable=False),
            sa.Column("capability_revision_id", sa.String(), nullable=False),
            sa.Column("capability_stage_criterion_id", sa.String(), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False),
            sa.Column("prompt", sa.Text(), nullable=False),
            sa.Column("task_context_json", sa.Text(), nullable=False),
            sa.Column("deliverables_json", sa.Text(), nullable=False),
            sa.Column("rubric_json", sa.Text(), nullable=False),
            sa.Column("reference_answer_json", sa.Text(), nullable=False),
            sa.Column("novelty_basis_json", sa.Text(), nullable=False),
            sa.Column("author_deployment_id", sa.String(length=160), nullable=False),
            sa.Column("author_model_family_id", sa.String(length=160), nullable=False),
            sa.Column("author_model", sa.String(length=160), nullable=False),
            sa.Column("provenance_mode", sa.String(length=40), nullable=False),
            sa.Column("publication_status", sa.String(length=24), nullable=False),
            sa.Column("task_hash", sa.String(length=64), nullable=False),
            sa.Column("authoring_rule_version", sa.String(length=48), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["section_id"], ["sections.id"]),
            sa.ForeignKeyConstraint(
                ["learning_contract_version_id"], ["learning_contract_versions.id"]
            ),
            sa.ForeignKeyConstraint(["content_version_id"], ["content_versions.id"]),
            sa.ForeignKeyConstraint(["assessment_target_id"], ["assessment_targets.id"]),
            sa.ForeignKeyConstraint(
                ["capability_revision_id"], ["capability_revisions.id"]
            ),
            sa.ForeignKeyConstraint(
                ["capability_stage_criterion_id"], ["capability_stage_criteria.id"]
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "learning_contract_version_id",
                "capability_stage_criterion_id",
                "version",
                name="uq_capability_application_task_contract_criterion_version",
            ),
            sa.UniqueConstraint(
                "task_hash", name="uq_capability_application_task_hash"
            ),
        )
        for column in (
            "section_id",
            "learning_contract_version_id",
            "content_version_id",
            "assessment_target_id",
            "capability_revision_id",
            "capability_stage_criterion_id",
            "publication_status",
            "created_at",
        ):
            _index("capability_application_task_versions", column)

    if "capability_application_submissions" not in tables:
        op.create_table(
            "capability_application_submissions",
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("task_version_id", sa.String(), nullable=False),
            sa.Column("learning_run_id", sa.String(), nullable=False),
            sa.Column("user_id", sa.String(), nullable=False),
            sa.Column("idempotency_key", sa.String(length=128), nullable=False),
            sa.Column("request_hash", sa.String(length=64), nullable=False),
            sa.Column("response_json", sa.Text(), nullable=False),
            sa.Column("assistance_mode", sa.String(length=32), nullable=False),
            sa.Column("status", sa.String(length=24), nullable=False),
            sa.Column("result_json", sa.Text(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(
                ["task_version_id"], ["capability_application_task_versions.id"]
            ),
            sa.ForeignKeyConstraint(["learning_run_id"], ["learning_runs.id"]),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
            sa.ForeignKeyConstraint(
                ["learning_run_id", "user_id"],
                ["learning_runs.id", "learning_runs.user_id"],
                name="fk_capability_application_submission_run_user",
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "learning_run_id",
                "user_id",
                "idempotency_key",
                name="uq_capability_application_submission_run_user_idempotency",
            ),
        )
        for column in (
            "task_version_id",
            "learning_run_id",
            "user_id",
            "status",
            "created_at",
        ):
            _index("capability_application_submissions", column)

    if "capability_application_evaluations" not in tables:
        op.create_table(
            "capability_application_evaluations",
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("submission_id", sa.String(), nullable=False),
            sa.Column("verdict", sa.String(length=24), nullable=False),
            sa.Column("evidence_sufficiency", sa.String(length=24), nullable=False),
            sa.Column("criterion_results_json", sa.Text(), nullable=False),
            sa.Column("rationale", sa.Text(), nullable=False),
            sa.Column("evaluator_deployment_id", sa.String(length=160), nullable=False),
            sa.Column("evaluator_model_family_id", sa.String(length=160), nullable=False),
            sa.Column("evaluator_model", sa.String(length=160), nullable=False),
            sa.Column("qualification_status", sa.String(length=24), nullable=False),
            sa.Column("qualification_reason", sa.Text(), nullable=False),
            sa.Column("evaluation_rule_version", sa.String(length=48), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(
                ["submission_id"], ["capability_application_submissions.id"]
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("submission_id"),
        )
        for column in (
            "submission_id",
            "verdict",
            "qualification_status",
            "created_at",
        ):
            _index("capability_application_evaluations", column)


def downgrade():
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    for table in (
        "capability_application_evaluations",
        "capability_application_submissions",
        "capability_application_task_versions",
    ):
        if table in tables:
            op.drop_table(table)
