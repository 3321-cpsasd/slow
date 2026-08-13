"""Add purpose-aware AI invocation routing lineage.

Revision ID: 0059_ai_gateway_lineage
Revises: 0058_reinforcement_agent
"""

from alembic import op
import sqlalchemy as sa


revision = "0059_ai_gateway_lineage"
down_revision = "0058_reinforcement_agent"
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
        ("purpose", sa.String(length=64), ""),
        ("authority", sa.String(length=32), ""),
        ("deployment_id", sa.String(length=160), ""),
        ("model_family_id", sa.String(length=80), ""),
        ("config_version_id", sa.String(length=80), ""),
        ("route_policy_version", sa.String(length=80), ""),
    )
    for name, kind, default in additions:
        if name not in columns:
            op.add_column(
                "ai_invocations",
                sa.Column(name, kind, nullable=False, server_default=default),
            )
    if "fallback_index" not in columns:
        op.add_column(
            "ai_invocations",
            sa.Column(
                "fallback_index",
                sa.Integer(),
                nullable=False,
                server_default="0",
            ),
        )
    for name in (
        "purpose",
        "deployment_id",
        "model_family_id",
        "config_version_id",
    ):
        index = f"ix_ai_invocations_{name}"
        existing = {
            item["name"]
            for item in sa.inspect(op.get_bind()).get_indexes("ai_invocations")
        }
        if index not in existing:
            op.create_index(index, "ai_invocations", [name])


def downgrade():
    for name in (
        "config_version_id",
        "model_family_id",
        "deployment_id",
        "purpose",
    ):
        op.drop_index(f"ix_ai_invocations_{name}", table_name="ai_invocations")
    for name in (
        "fallback_index",
        "route_policy_version",
        "config_version_id",
        "model_family_id",
        "deployment_id",
        "authority",
        "purpose",
    ):
        op.drop_column("ai_invocations", name)
