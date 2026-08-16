"""Merge capability-profile and reading-annotation migration branches.

Revision ID: 0074_merge_capabilities_annotations
Revises: 0073_capability_review_tasks, 0066_reading_annotations
"""


revision = "0074_merge_capabilities_annotations"
down_revision = (
    "0073_capability_review_tasks",
    "0066_reading_annotations",
)
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
