"""Add required, resumable baseline profile onboarding.

Revision ID: 0025_profile_onboarding
Revises: 0024_version_remediations
"""

from alembic import op
import sqlalchemy as sa


revision = "0025_profile_onboarding"
down_revision = "0024_version_remediations"
branch_labels = None
depends_on = None


def upgrade():
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    if "user_profiles" not in existing:
        op.create_table(
        "user_profiles",
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("profession", sa.String(120), nullable=False, server_default=""),
        sa.Column("stage", sa.String(40), nullable=False, server_default=""),
        sa.Column("purpose", sa.Text(), nullable=False, server_default=""),
        sa.Column("domains_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("experience", sa.Text(), nullable=False, server_default=""),
        sa.Column("version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("user_id"),
        )
    if "user_profile_revisions" not in existing:
        op.create_table(
        "user_profile_revisions",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("snapshot_json", sa.Text(), nullable=False),
        sa.Column("source", sa.String(40), nullable=False, server_default="self_report"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id",
            "version",
            name="uq_user_profile_revisions_user_version",
        ),
        )
        op.create_index(
            "ix_user_profile_revisions_user_id",
            "user_profile_revisions",
            ["user_id"],
            unique=False,
        )
    if "user_onboardings" not in existing:
        op.create_table(
        "user_onboardings",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("flow_id", sa.String(80), nullable=False),
        sa.Column("flow_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("status", sa.String(24), nullable=False, server_default="required"),
        sa.Column("current_step", sa.String(80), nullable=False, server_default="identity"),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id",
            "flow_id",
            name="uq_user_onboardings_user_flow",
        ),
        )
        op.create_index(
            "ix_user_onboardings_user_id",
            "user_onboardings",
            ["user_id"],
            unique=False,
        )
        op.create_index(
            "ix_user_onboardings_status",
            "user_onboardings",
            ["status"],
            unique=False,
        )


def downgrade():
    op.drop_index("ix_user_onboardings_status", table_name="user_onboardings")
    op.drop_index("ix_user_onboardings_user_id", table_name="user_onboardings")
    op.drop_table("user_onboardings")
    op.drop_index(
        "ix_user_profile_revisions_user_id",
        table_name="user_profile_revisions",
    )
    op.drop_table("user_profile_revisions")
    op.drop_table("user_profiles")
