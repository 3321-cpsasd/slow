"""Add server-owned delayed-review assignment lifecycle.

Revision ID: 0033_review_assignments
Revises: 0032_content_governance
"""

from alembic import op
import sqlalchemy as sa


revision = "0033_review_assignments"
down_revision = "0032_content_governance"
branch_labels = None
depends_on = None


def upgrade():
    # Some recovery tests (and developer databases created from current ORM
    # metadata) may already contain the complete future schema while Alembic's
    # version is older.  In that case the tables themselves are authoritative
    # and this additive migration is already satisfied.
    if "review_selection_runs" in set(sa.inspect(op.get_bind()).get_table_names()):
        return
    op.create_table(
        "review_selection_runs",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("user_id", sa.String(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("selection_date", sa.String(length=10), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("daily_budget", sa.Integer(), nullable=False),
        sa.Column("due_count", sa.Integer(), nullable=False),
        sa.Column("rule_version", sa.String(length=40), nullable=False),
        sa.Column("input_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "user_id",
            "selection_date",
            "rule_version",
            name="uq_review_selection_runs_user_day_rule",
        ),
    )
    op.create_index("ix_review_selection_runs_user_id", "review_selection_runs", ["user_id"])
    op.create_index("ix_review_selection_runs_selection_date", "review_selection_runs", ["selection_date"])
    op.create_index("ix_review_selection_runs_rule_version", "review_selection_runs", ["rule_version"])

    op.create_table(
        "review_assignments",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column(
            "selection_run_id",
            sa.String(),
            sa.ForeignKey("review_selection_runs.id"),
            nullable=False,
        ),
        sa.Column("review_state_id", sa.String(), sa.ForeignKey("review_states.id"), nullable=False),
        sa.Column("user_id", sa.String(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column(
            "assessment_target_id",
            sa.String(),
            sa.ForeignKey("assessment_targets.id"),
            nullable=False,
        ),
        sa.Column(
            "source_learning_run_id",
            sa.String(),
            sa.ForeignKey("learning_runs.id"),
            nullable=False,
        ),
        sa.Column("source_section_id", sa.String(), sa.ForeignKey("sections.id"), nullable=False),
        sa.Column(
            "learning_contract_version_id",
            sa.String(),
            sa.ForeignKey("learning_contract_versions.id"),
            nullable=False,
        ),
        sa.Column(
            "content_version_id",
            sa.String(),
            sa.ForeignKey("content_versions.id"),
            nullable=False,
        ),
        sa.Column("prior_quiz_set_id", sa.String(), sa.ForeignKey("quiz_sets.id"), nullable=False),
        sa.Column("review_quiz_set_id", sa.String(), sa.ForeignKey("quiz_sets.id"), nullable=True),
        sa.Column("submitted_attempt_id", sa.String(), sa.ForeignKey("quiz_attempts.id"), nullable=True),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("base_priority", sa.Integer(), nullable=False),
        sa.Column("effective_priority", sa.Integer(), nullable=False),
        sa.Column("selection_rule_version", sa.String(length=40), nullable=False),
        sa.Column("qualification_rule_version", sa.String(length=40), nullable=False),
        sa.Column("prior_item_signatures_json", sa.Text(), nullable=False),
        sa.Column("item_signatures_json", sa.Text(), nullable=False),
        sa.Column("response_json", sa.Text(), nullable=False),
        sa.Column("last_event_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "selection_run_id",
            "assessment_target_id",
            name="uq_review_assignments_selection_target",
        ),
        sa.UniqueConstraint("review_quiz_set_id", name="uq_review_assignments_review_quiz_set_id"),
        sa.UniqueConstraint("submitted_attempt_id", name="uq_review_assignments_submitted_attempt_id"),
    )
    for column in (
        "selection_run_id",
        "review_state_id",
        "user_id",
        "assessment_target_id",
        "source_learning_run_id",
        "source_section_id",
        "learning_contract_version_id",
        "content_version_id",
        "prior_quiz_set_id",
        "due_at",
        "expires_at",
        "status",
    ):
        op.create_index(f"ix_review_assignments_{column}", "review_assignments", [column])

    op.create_table(
        "review_assignment_events",
        sa.Column("sequence", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("id", sa.String(), nullable=False),
        sa.Column(
            "assignment_id",
            sa.String(),
            sa.ForeignKey("review_assignments.id"),
            nullable=False,
        ),
        sa.Column("event_type", sa.String(length=24), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("rule_version", sa.String(length=40), nullable=False),
        sa.Column("idempotency_key", sa.String(length=160), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("id", name="uq_review_assignment_events_id"),
        sa.UniqueConstraint(
            "assignment_id",
            "event_type",
            name="uq_review_assignment_events_type",
        ),
    )
    op.create_index("ix_review_assignment_events_id", "review_assignment_events", ["id"])
    op.create_index("ix_review_assignment_events_assignment_id", "review_assignment_events", ["assignment_id"])
    op.create_index("ix_review_assignment_events_event_type", "review_assignment_events", ["event_type"])


def downgrade():
    op.drop_table("review_assignment_events")
    op.drop_table("review_assignments")
    op.drop_table("review_selection_runs")
