"""Freeze chapter-level published knowledge identity scope.

Revision ID: 0044_chapter_knowledge_identity_scope
Revises: 0043_knowledge_fact_graph
"""

from alembic import op
import sqlalchemy as sa


revision = "0044_chapter_knowledge_identity_scope"
down_revision = "0043_knowledge_fact_graph"
branch_labels = None
depends_on = None


def upgrade():
    columns = {
        item["name"] for item in sa.inspect(op.get_bind()).get_columns("chapters")
    }
    if "knowledge_identity_scope_json" not in columns:
        op.add_column(
            "chapters",
            sa.Column(
                "knowledge_identity_scope_json",
                sa.Text(),
                nullable=False,
                server_default="{}",
            ),
        )


def downgrade():
    columns = {
        item["name"] for item in sa.inspect(op.get_bind()).get_columns("chapters")
    }
    if "knowledge_identity_scope_json" in columns:
        op.drop_column("chapters", "knowledge_identity_scope_json")
