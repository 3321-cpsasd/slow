"""Freeze stage-aware review and strengthening task plans.

Revision ID: 0072_review_task_plans
Revises: 0071_formal_transfer_tasks
"""

from alembic import op
import sqlalchemy as sa


revision = "0072_review_task_plans"
down_revision = "0071_formal_transfer_tasks"
branch_labels = None
depends_on = None


def upgrade():
    columns = {
        item["name"]
        for item in sa.inspect(op.get_bind()).get_columns("review_assignments")
    }
    if "task_plan_json" not in columns:
        op.add_column(
            "review_assignments",
            sa.Column("task_plan_json", sa.Text(), nullable=False, server_default="{}"),
        )
    if "task_plan_rule_version" not in columns:
        op.add_column(
            "review_assignments",
            sa.Column(
                "task_plan_rule_version",
                sa.String(length=48),
                nullable=False,
                server_default="review_task_plan_v1",
            ),
        )
    indexes = {
        item["name"]
        for item in sa.inspect(op.get_bind()).get_indexes("review_assignments")
    }
    if "ix_review_assignments_task_plan_rule_version" not in indexes:
        op.create_index(
            "ix_review_assignments_task_plan_rule_version",
            "review_assignments",
            ["task_plan_rule_version"],
        )


def downgrade():
    indexes = {
        item["name"]
        for item in sa.inspect(op.get_bind()).get_indexes("review_assignments")
    }
    if "ix_review_assignments_task_plan_rule_version" in indexes:
        op.drop_index(
            "ix_review_assignments_task_plan_rule_version",
            table_name="review_assignments",
        )
    columns = {
        item["name"]
        for item in sa.inspect(op.get_bind()).get_columns("review_assignments")
    }
    for name in ("task_plan_rule_version", "task_plan_json"):
        if name in columns:
            op.drop_column("review_assignments", name)
