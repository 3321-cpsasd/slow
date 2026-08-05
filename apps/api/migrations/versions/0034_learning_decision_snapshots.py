"""Add immutable assessment-gate and progression decision snapshots.

Revision ID: 0034_learning_decision_snapshots
Revises: 0033_review_assignments
"""

from alembic import op
import sqlalchemy as sa


revision = "0034_learning_decision_snapshots"
down_revision = "0033_review_assignments"
branch_labels = None
depends_on = None


def _tables():
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade():
    # ``Base.metadata.create_all`` is used by development/test bootstraps before
    # Alembic stamps older schemas.  Treat an ORM-created future table as already
    # satisfied, matching the compatibility posture of preceding migrations.
    if "learning_decision_snapshots" in _tables():
        return
    op.create_table(
        "learning_decision_snapshots",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("learning_run_id", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("section_id", sa.String(), nullable=False),
        sa.Column("attempt_id", sa.String(), nullable=False),
        sa.Column("learning_contract_version_id", sa.String(), nullable=True),
        sa.Column("content_version_id", sa.String(), nullable=True),
        sa.Column("decision_kind", sa.String(length=32), nullable=False),
        sa.Column("trigger_kind", sa.String(length=32), nullable=False),
        sa.Column("rule_version", sa.String(length=40), nullable=False),
        sa.Column("input_snapshot_json", sa.Text(), nullable=False),
        sa.Column("output_decision_json", sa.Text(), nullable=False),
        sa.Column(
            "source_observation_watermark",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("input_hash", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["learning_run_id", "user_id"],
            ["learning_runs.id", "learning_runs.user_id"],
            name="fk_learning_decision_snapshots_run_user",
        ),
        sa.ForeignKeyConstraint(["learning_run_id"], ["learning_runs.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["section_id"], ["sections.id"]),
        sa.ForeignKeyConstraint(["attempt_id"], ["quiz_attempts.id"]),
        sa.ForeignKeyConstraint(
            ["learning_contract_version_id"],
            ["learning_contract_versions.id"],
        ),
        sa.ForeignKeyConstraint(["content_version_id"], ["content_versions.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "decision_kind",
            "idempotency_key",
            name="uq_learning_decision_kind_idempotency",
        ),
    )
    for column in (
        "learning_run_id",
        "user_id",
        "section_id",
        "attempt_id",
        "learning_contract_version_id",
        "content_version_id",
        "decision_kind",
        "trigger_kind",
    ):
        op.create_index(
            f"ix_learning_decision_snapshots_{column}",
            "learning_decision_snapshots",
            [column],
        )


def downgrade():
    if "learning_decision_snapshots" in _tables():
        op.drop_table("learning_decision_snapshots")
