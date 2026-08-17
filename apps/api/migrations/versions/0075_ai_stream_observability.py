"""Add safe stream timing and completion observations to AI invocations.

Revision ID: 0075_ai_stream_observability
Revises: 0074_merge_capabilities_annotations
"""

from alembic import op
import sqlalchemy as sa


revision = "0075_ai_stream_observability"
down_revision = "0074_merge_capabilities_annotations"
branch_labels = None
depends_on = None


def _columns(table_name: str) -> set[str]:
    return {
        item["name"]
        for item in sa.inspect(op.get_bind()).get_columns(table_name)
    }


def upgrade():
    columns = _columns("ai_invocations")
    additions = (
        ("streamed", sa.Boolean(), "0"),
        ("stream_chunk_count", sa.Integer(), "0"),
        ("stream_content_chars", sa.Integer(), "0"),
        ("stream_reasoning_chars", sa.Integer(), "0"),
        ("stream_finish_reason", sa.String(length=40), ""),
    )
    for name, kind, default in additions:
        if name not in columns:
            op.add_column(
                "ai_invocations",
                sa.Column(name, kind, nullable=False, server_default=default),
            )
    for name in ("first_event_at", "first_content_at", "last_event_at"):
        if name not in columns:
            op.add_column(
                "ai_invocations",
                sa.Column(name, sa.DateTime(timezone=True), nullable=True),
            )


def downgrade():
    columns = _columns("ai_invocations")
    for name in (
        "stream_finish_reason",
        "stream_reasoning_chars",
        "stream_content_chars",
        "stream_chunk_count",
        "last_event_at",
        "first_content_at",
        "first_event_at",
        "streamed",
    ):
        if name in columns:
            op.drop_column("ai_invocations", name)
