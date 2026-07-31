"""Add OIDC identities and revocable server-side sessions.

Revision ID: 0019_identity_sessions
Revises: 0018_multi_user_state_authority
"""

from alembic import op
import sqlalchemy as sa


revision = "0019_identity_sessions"
down_revision = "0018_multi_user_state_authority"
branch_labels = None
depends_on = None


def upgrade():
    connection = op.get_bind()
    inspector = sa.inspect(connection)
    if not inspector.has_table("user_identities"):
        op.create_table(
            "user_identities",
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("user_id", sa.String(), nullable=False),
            sa.Column("issuer", sa.String(length=500), nullable=False),
            sa.Column("subject", sa.String(length=500), nullable=False),
            sa.Column(
                "email_snapshot",
                sa.String(length=320),
                nullable=False,
                server_default="",
            ),
            sa.Column(
                "email_verified",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            ),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "issuer",
                "subject",
                name="uq_user_identities_issuer_subject",
            ),
        )
    if "ix_user_identities_user_id" not in {
        item["name"] for item in sa.inspect(connection).get_indexes("user_identities")
    }:
        op.create_index(
            "ix_user_identities_user_id",
            "user_identities",
            ["user_id"],
            unique=False,
        )

    if not sa.inspect(connection).has_table("auth_sessions"):
        op.create_table(
            "auth_sessions",
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("user_id", sa.String(), nullable=False),
            sa.Column("token_hash", sa.String(length=64), nullable=False),
            sa.Column("csrf_token_hash", sa.String(length=64), nullable=False),
            sa.Column(
                "status",
                sa.String(length=24),
                nullable=False,
                server_default="active",
            ),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
    auth_indexes = {
        item["name"] for item in sa.inspect(connection).get_indexes("auth_sessions")
    }
    for name, columns, unique in (
        ("ix_auth_sessions_user_id", ["user_id"], False),
        ("ix_auth_sessions_token_hash", ["token_hash"], True),
        ("ix_auth_sessions_status", ["status"], False),
        ("ix_auth_sessions_expires_at", ["expires_at"], False),
    ):
        if name not in auth_indexes:
            op.create_index(name, "auth_sessions", columns, unique=unique)

    if not sa.inspect(connection).has_table("oidc_login_states"):
        op.create_table(
            "oidc_login_states",
            sa.Column("state_hash", sa.String(length=64), nullable=False),
            sa.Column("nonce", sa.String(length=160), nullable=False),
            sa.Column("code_verifier", sa.String(length=160), nullable=False),
            sa.Column(
                "return_to",
                sa.String(length=1000),
                nullable=False,
                server_default="/",
            ),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("state_hash"),
        )
    if "ix_oidc_login_states_expires_at" not in {
        item["name"]
        for item in sa.inspect(connection).get_indexes("oidc_login_states")
    }:
        op.create_index(
            "ix_oidc_login_states_expires_at",
            "oidc_login_states",
            ["expires_at"],
            unique=False,
        )


def downgrade():
    op.drop_index(
        "ix_oidc_login_states_expires_at",
        table_name="oidc_login_states",
    )
    op.drop_table("oidc_login_states")
    op.drop_index("ix_auth_sessions_expires_at", table_name="auth_sessions")
    op.drop_index("ix_auth_sessions_status", table_name="auth_sessions")
    op.drop_index("ix_auth_sessions_token_hash", table_name="auth_sessions")
    op.drop_index("ix_auth_sessions_user_id", table_name="auth_sessions")
    op.drop_table("auth_sessions")
    op.drop_index(
        "ix_user_identities_user_id",
        table_name="user_identities",
    )
    op.drop_table("user_identities")
