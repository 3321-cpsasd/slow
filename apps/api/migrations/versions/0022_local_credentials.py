"""Add development-only local password credentials.

Revision ID: 0022_local_credentials
Revises: 0021_artifact_submission_facts
"""

from alembic import op
import sqlalchemy as sa


revision = "0022_local_credentials"
down_revision = "0021_artifact_submission_facts"
branch_labels = None
depends_on = None


def upgrade():
    connection = op.get_bind()
    if not sa.inspect(connection).has_table("local_credentials"):
        op.create_table(
            "local_credentials",
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("user_id", sa.String(), nullable=False),
            sa.Column("username", sa.String(length=80), nullable=False),
            sa.Column("password_hash", sa.Text(), nullable=False),
            sa.Column(
                "status",
                sa.String(length=24),
                nullable=False,
                server_default="active",
            ),
            sa.Column(
                "failed_attempts",
                sa.Integer(),
                nullable=False,
                server_default="0",
            ),
            sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "password_changed_at",
                sa.DateTime(timezone=True),
                nullable=False,
            ),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("user_id"),
            sa.UniqueConstraint("username"),
        )
    existing_indexes = {
        item["name"]
        for item in sa.inspect(connection).get_indexes("local_credentials")
    }
    for name, columns, unique in (
        ("ix_local_credentials_user_id", ["user_id"], True),
        ("ix_local_credentials_username", ["username"], True),
        ("ix_local_credentials_status", ["status"], False),
        ("ix_local_credentials_locked_until", ["locked_until"], False),
    ):
        if name not in existing_indexes:
            op.create_index(
                name,
                "local_credentials",
                columns,
                unique=unique,
            )


def downgrade():
    for name in (
        "ix_local_credentials_locked_until",
        "ix_local_credentials_status",
        "ix_local_credentials_username",
        "ix_local_credentials_user_id",
    ):
        op.drop_index(name, table_name="local_credentials")
    op.drop_table("local_credentials")
