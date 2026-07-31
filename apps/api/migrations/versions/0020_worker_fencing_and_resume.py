"""Fence durable workers and persist cross-device reading position.

Revision ID: 0020_worker_fencing_and_resume
Revises: 0019_identity_sessions
"""

from alembic import op
import sqlalchemy as sa


revision = "0020_worker_fencing_and_resume"
down_revision = "0019_identity_sessions"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table(
        "learning_tasks",
        recreate="always",
    ) as batch_op:
        batch_op.add_column(
            sa.Column("lease_owner", sa.String(length=160), nullable=True)
        )
        batch_op.add_column(
            sa.Column("lease_token", sa.String(length=160), nullable=True)
        )
        batch_op.add_column(
            sa.Column(
                "lease_expires_at",
                sa.DateTime(timezone=True),
                nullable=True,
            )
        )
        batch_op.add_column(
            sa.Column(
                "heartbeat_at",
                sa.DateTime(timezone=True),
                nullable=True,
            )
        )
        batch_op.create_unique_constraint(
            "uq_learning_tasks_lease_token",
            ["lease_token"],
        )
    op.create_index(
        "ix_learning_tasks_lease_owner",
        "learning_tasks",
        ["lease_owner"],
        unique=False,
    )
    op.create_index(
        "ix_learning_tasks_lease_expires_at",
        "learning_tasks",
        ["lease_expires_at"],
        unique=False,
    )
    op.execute(
        """
        UPDATE learning_tasks
        SET lease_expires_at = updated_at
        WHERE status = 'running' AND lease_expires_at IS NULL
        """
    )

    op.create_table(
        "learning_resume_positions",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("learning_run_id", sa.String(), nullable=False),
        sa.Column("section_id", sa.String(), nullable=False),
        sa.Column(
            "block_id",
            sa.String(length=200),
            nullable=False,
            server_default="",
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["learning_run_id"], ["learning_runs.id"]),
        sa.ForeignKeyConstraint(["section_id"], ["sections.id"]),
        sa.ForeignKeyConstraint(
            ["learning_run_id", "user_id"],
            ["learning_runs.id", "learning_runs.user_id"],
            name="fk_learning_resume_run_user",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id",
            "learning_run_id",
            name="uq_learning_resume_user_run",
        ),
    )
    op.create_index(
        "ix_learning_resume_positions_user_id",
        "learning_resume_positions",
        ["user_id"],
        unique=False,
    )
    op.create_index(
        "ix_learning_resume_positions_learning_run_id",
        "learning_resume_positions",
        ["learning_run_id"],
        unique=False,
    )
    op.create_index(
        "ix_learning_resume_positions_section_id",
        "learning_resume_positions",
        ["section_id"],
        unique=False,
    )
    op.create_index(
        "ix_learning_resume_positions_updated_at",
        "learning_resume_positions",
        ["updated_at"],
        unique=False,
    )


def downgrade():
    op.drop_index(
        "ix_learning_resume_positions_updated_at",
        table_name="learning_resume_positions",
    )
    op.drop_index(
        "ix_learning_resume_positions_section_id",
        table_name="learning_resume_positions",
    )
    op.drop_index(
        "ix_learning_resume_positions_learning_run_id",
        table_name="learning_resume_positions",
    )
    op.drop_index(
        "ix_learning_resume_positions_user_id",
        table_name="learning_resume_positions",
    )
    op.drop_table("learning_resume_positions")

    op.drop_index(
        "ix_learning_tasks_lease_expires_at",
        table_name="learning_tasks",
    )
    op.drop_index(
        "ix_learning_tasks_lease_owner",
        table_name="learning_tasks",
    )
    with op.batch_alter_table(
        "learning_tasks",
        recreate="always",
    ) as batch_op:
        batch_op.drop_constraint(
            "uq_learning_tasks_lease_token",
            type_="unique",
        )
        batch_op.drop_column("heartbeat_at")
        batch_op.drop_column("lease_expires_at")
        batch_op.drop_column("lease_token")
        batch_op.drop_column("lease_owner")
