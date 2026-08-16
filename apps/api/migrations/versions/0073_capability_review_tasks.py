"""Add immutable high-stage capability review task facts.

Revision ID: 0073_capability_review_tasks
Revises: 0072_review_task_plans
"""

from alembic import op
import sqlalchemy as sa


revision = "0073_capability_review_tasks"
down_revision = "0072_review_task_plans"
branch_labels = None
depends_on = None


def _index(table: str, column: str, name: str | None = None) -> None:
    op.create_index(name or f"ix_{table}_{column}", table, [column])


def upgrade():
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "capability_review_task_versions" not in tables:
        op.create_table(
            "capability_review_task_versions",
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("review_assignment_id", sa.String(), nullable=False),
            sa.Column("assessment_target_id", sa.String(), nullable=False),
            sa.Column("capability_revision_id", sa.String(), nullable=False),
            sa.Column("task_kind", sa.String(length=32), nullable=False),
            sa.Column("stage", sa.String(length=24), nullable=False),
            sa.Column("criterion_ids_json", sa.Text(), nullable=False),
            sa.Column("prompt", sa.Text(), nullable=False),
            sa.Column("task_context_json", sa.Text(), nullable=False),
            sa.Column("deliverables_json", sa.Text(), nullable=False),
            sa.Column("rubric_json", sa.Text(), nullable=False),
            sa.Column("reference_answer_json", sa.Text(), nullable=False),
            sa.Column("novelty_basis_json", sa.Text(), nullable=False),
            sa.Column("required_knowledge_json", sa.Text(), nullable=False),
            sa.Column("author_deployment_id", sa.String(length=160), nullable=False),
            sa.Column("author_model_family_id", sa.String(length=160), nullable=False),
            sa.Column("author_model", sa.String(length=160), nullable=False),
            sa.Column("provenance_mode", sa.String(length=40), nullable=False),
            sa.Column("publication_status", sa.String(length=24), nullable=False),
            sa.Column("task_hash", sa.String(length=64), nullable=False),
            sa.Column("rule_version", sa.String(length=48), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["review_assignment_id"], ["review_assignments.id"]),
            sa.ForeignKeyConstraint(["assessment_target_id"], ["assessment_targets.id"]),
            sa.ForeignKeyConstraint(["capability_revision_id"], ["capability_revisions.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("review_assignment_id"),
            sa.UniqueConstraint("task_hash"),
        )
        for column in ("review_assignment_id", "assessment_target_id", "capability_revision_id", "task_kind", "stage", "publication_status", "created_at"):
            _index("capability_review_task_versions", column, f"ix_cap_review_task_{column}")
    if "capability_review_submissions" not in tables:
        op.create_table(
            "capability_review_submissions",
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("review_task_version_id", sa.String(), nullable=False),
            sa.Column("user_id", sa.String(), nullable=False),
            sa.Column("idempotency_key", sa.String(length=128), nullable=False),
            sa.Column("request_hash", sa.String(length=64), nullable=False),
            sa.Column("response_json", sa.Text(), nullable=False),
            sa.Column("assistance_mode", sa.String(length=32), nullable=False),
            sa.Column("status", sa.String(length=24), nullable=False),
            sa.Column("result_json", sa.Text(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["review_task_version_id"], ["capability_review_task_versions.id"]),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("review_task_version_id"),
            sa.UniqueConstraint("review_task_version_id", "idempotency_key", name="uq_capability_review_submission_task_idempotency"),
        )
        for column in ("review_task_version_id", "user_id", "status", "created_at"):
            _index("capability_review_submissions", column, f"ix_cap_review_submission_{column}")
    if "capability_review_evaluations" not in tables:
        op.create_table(
            "capability_review_evaluations",
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
            sa.Column("rule_version", sa.String(length=48), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["submission_id"], ["capability_review_submissions.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("submission_id"),
        )
        for column in ("submission_id", "verdict", "qualification_status", "created_at"):
            _index("capability_review_evaluations", column, f"ix_cap_review_evaluation_{column}")


def downgrade():
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    for table in ("capability_review_evaluations", "capability_review_submissions", "capability_review_task_versions"):
        if table in tables:
            op.drop_table(table)
