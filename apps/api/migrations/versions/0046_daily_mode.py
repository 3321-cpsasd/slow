"""Add cross-device daily learning mode state and audit events.

Revision ID: 0046_daily_mode
Revises: 0045_ask_me_discussions
"""

from alembic import op
import sqlalchemy as sa


revision = "0046_daily_mode"
down_revision = "0045_ask_me_discussions"
branch_labels = None
depends_on = None


def upgrade():
    required_tables = {"user_daily_mode_states", "daily_mode_events"}
    existing_tables = set(sa.inspect(op.get_bind()).get_table_names())
    partial = required_tables & existing_tables
    if partial and not required_tables.issubset(existing_tables):
        raise RuntimeError(
            "partial daily mode schema requires explicit repair: "
            + ", ".join(sorted(partial))
        )

    if not required_tables.issubset(existing_tables):
        _create_daily_mode_tables()

    qa_columns = {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns("qa_sessions")
    }
    if "daily_mode" not in qa_columns:
        with op.batch_alter_table("qa_sessions") as batch:
            batch.add_column(
                sa.Column(
                    "daily_mode",
                    sa.String(16),
                    nullable=False,
                    server_default="slow",
                )
            )


def _create_daily_mode_tables():
    op.create_table(
        "user_daily_mode_states",
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("daily_mode", sa.String(16), nullable=False),
        sa.Column("duration", sa.String(16), nullable=False),
        sa.Column("timezone", sa.String(64), nullable=False),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("user_id"),
    )
    op.create_index(
        "ix_user_daily_mode_states_daily_mode",
        "user_daily_mode_states",
        ["daily_mode"],
    )
    op.create_index(
        "ix_user_daily_mode_states_expires_at",
        "user_daily_mode_states",
        ["expires_at"],
    )

    op.create_table(
        "daily_mode_events",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("previous_mode", sa.String(16), nullable=True),
        sa.Column("daily_mode", sa.String(16), nullable=False),
        sa.Column("duration", sa.String(16), nullable=False),
        sa.Column("timezone", sa.String(64), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("state_version", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id",
            "idempotency_key",
            name="uq_daily_mode_events_user_idempotency",
        ),
    )
    for column in (
        "user_id",
        "daily_mode",
        "source",
        "expires_at",
        "created_at",
    ):
        op.create_index(
            f"ix_daily_mode_events_{column}",
            "daily_mode_events",
            [column],
        )


def downgrade():
    with op.batch_alter_table("qa_sessions") as batch:
        batch.drop_column("daily_mode")
    op.drop_table("daily_mode_events")
    op.drop_table("user_daily_mode_states")
