"""Add versioned milestone paths and long-term goal pacing.

Revision ID: 0027_milestone_paths
Revises: 0026_shelf_origin_cleanup
"""

from alembic import op
import sqlalchemy as sa


revision = "0027_milestone_paths"
down_revision = "0026_shelf_origin_cleanup"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    profile_columns = {
        column["name"] for column in inspector.get_columns("user_profiles")
    }
    if "weekly_minutes" not in profile_columns:
        op.add_column(
            "user_profiles",
            sa.Column(
                "weekly_minutes",
                sa.Integer(),
                nullable=False,
                server_default="0",
            ),
        )
    if "target_date" not in profile_columns:
        op.add_column(
            "user_profiles",
            sa.Column(
                "target_date",
                sa.String(10),
                nullable=False,
                server_default="",
            ),
        )

    tables = set(sa.inspect(bind).get_table_names())
    if "milestone_paths" not in tables:
        op.create_table(
            "milestone_paths",
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("user_id", sa.String(), nullable=False),
            sa.Column("series_id", sa.String(), nullable=False),
            sa.Column("goal_profile_version", sa.Integer(), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("status", sa.String(24), nullable=False, server_default="proposed"),
            sa.Column("definition_json", sa.Text(), nullable=False),
            sa.Column("ruleset_version", sa.String(40), nullable=False, server_default="milestone_v1"),
            sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["series_id"], ["series.id"]),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("series_id"),
        )
        op.create_index("ix_milestone_paths_user_id", "milestone_paths", ["user_id"], unique=False)
        op.create_index("ix_milestone_paths_series_id", "milestone_paths", ["series_id"], unique=True)
        op.create_index("ix_milestone_paths_status", "milestone_paths", ["status"], unique=False)

    if "milestone_path_revisions" not in tables:
        op.create_table(
            "milestone_path_revisions",
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("path_id", sa.String(), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False),
            sa.Column("snapshot_json", sa.Text(), nullable=False),
            sa.Column("source", sa.String(40), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["path_id"], ["milestone_paths.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "path_id",
                "version",
                name="uq_milestone_path_revisions_path_version",
            ),
        )
        op.create_index(
            "ix_milestone_path_revisions_path_id",
            "milestone_path_revisions",
            ["path_id"],
            unique=False,
        )


def downgrade():
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "milestone_path_revisions" in tables:
        op.drop_index(
            "ix_milestone_path_revisions_path_id",
            table_name="milestone_path_revisions",
        )
        op.drop_table("milestone_path_revisions")
    if "milestone_paths" in tables:
        op.drop_index("ix_milestone_paths_status", table_name="milestone_paths")
        op.drop_index("ix_milestone_paths_series_id", table_name="milestone_paths")
        op.drop_index("ix_milestone_paths_user_id", table_name="milestone_paths")
        op.drop_table("milestone_paths")
    profile_columns = {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns("user_profiles")
    }
    if "target_date" in profile_columns:
        op.drop_column("user_profiles", "target_date")
    if "weekly_minutes" in profile_columns:
        op.drop_column("user_profiles", "weekly_minutes")
