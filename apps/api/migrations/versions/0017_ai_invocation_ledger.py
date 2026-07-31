"""Add the system-level AI invocation and usage ledger.

Revision ID: 0017_ai_invocation_ledger
Revises: 0016_generation_leases
"""

from alembic import op
import sqlalchemy as sa


revision = "0017_ai_invocation_ledger"
down_revision = "0016_generation_leases"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "ai_invocations",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("api_mode", sa.String(length=32), nullable=False),
        sa.Column("model", sa.String(length=160), nullable=False),
        sa.Column("operation", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("usage_status", sa.String(length=24), nullable=False),
        sa.Column("attribution_status", sa.String(length=32), nullable=False),
        sa.Column("actor_kind", sa.String(length=32), nullable=False),
        sa.Column("actor_id", sa.String(length=160), nullable=False),
        sa.Column("subject_user_id", sa.String(), nullable=True),
        sa.Column("provider_response_id", sa.String(length=200), nullable=False),
        sa.Column("error_code", sa.String(length=80), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("metering_schema_version", sa.String(length=24), nullable=False),
        sa.ForeignKeyConstraint(["subject_user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    for column in ("provider", "model", "operation", "status", "usage_status", "attribution_status", "subject_user_id"):
        op.create_index(op.f(f"ix_ai_invocations_{column}"), "ai_invocations", [column])

    op.create_table(
        "ai_usage_measurements",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("invocation_id", sa.String(), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("quality", sa.String(length=24), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("cached_input_tokens", sa.Integer(), nullable=True),
        sa.Column("cache_write_5m_tokens", sa.Integer(), nullable=True),
        sa.Column("cache_write_1h_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("reasoning_tokens", sa.Integer(), nullable=True),
        sa.Column("total_tokens", sa.Integer(), nullable=True),
        sa.Column("raw_usage_json", sa.Text(), nullable=False),
        sa.Column("measurement_version", sa.String(length=24), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["invocation_id"], ["ai_invocations.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "invocation_id",
            "source",
            "measurement_version",
            name="uq_ai_usage_measurement_source_version",
        ),
    )
    for column in ("invocation_id", "source", "quality"):
        op.create_index(op.f(f"ix_ai_usage_measurements_{column}"), "ai_usage_measurements", [column])


def downgrade():
    op.drop_table("ai_usage_measurements")
    op.drop_table("ai_invocations")
