"""Add privacy-scoped first-party product events.

Revision ID: 0041_product_events
Revises: 0040_privacy_and_account_exit
"""

from alembic import op
import sqlalchemy as sa


revision = "0041_product_events"
down_revision = "0040_privacy_and_account_exit"
branch_labels = None
depends_on = None


def upgrade():
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "product_events" in tables:
        return
    op.create_table(
        "product_events",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("event_id", sa.String(80), nullable=False),
        sa.Column("session_id", sa.String(80), nullable=False),
        sa.Column("event_name", sa.String(64), nullable=False),
        sa.Column("page_path", sa.String(500), nullable=False),
        sa.Column("view", sa.String(40), nullable=False),
        sa.Column("entity_type", sa.String(40), nullable=False),
        sa.Column("entity_id", sa.String(160), nullable=False),
        sa.Column("properties_json", sa.Text(), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("source", sa.String(24), nullable=False),
        sa.Column("schema_version", sa.String(24), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id",
            "event_id",
            name="uq_product_events_user_event",
        ),
    )
    for column in (
        "user_id",
        "session_id",
        "event_name",
        "occurred_at",
        "received_at",
    ):
        op.create_index(op.f(f"ix_product_events_{column}"), "product_events", [column])


def downgrade():
    op.drop_table("product_events")
