"""Add database-backed leases for AI generation.

Revision ID: 0016_generation_leases
Revises: 0015_initial_lesson_preload
"""

from alembic import op
import sqlalchemy as sa


revision = "0016_generation_leases"
down_revision = "0015_initial_lesson_preload"
branch_labels = None
depends_on = None


def upgrade():
    connection = op.get_bind()
    inspector = sa.inspect(connection)
    if "generation_leases" not in inspector.get_table_names():
        op.create_table(
            "generation_leases",
            sa.Column("resource_key", sa.String(length=200), nullable=False),
            sa.Column("owner_id", sa.String(length=80), nullable=False),
            sa.Column("acquired_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("resource_key"),
            sa.UniqueConstraint("owner_id"),
        )
        inspector = sa.inspect(connection)
    index_name = op.f("ix_generation_leases_expires_at")
    existing_indexes = {
        item["name"] for item in inspector.get_indexes("generation_leases")
    }
    if index_name not in existing_indexes:
        op.create_index(
            index_name,
            "generation_leases",
            ["expires_at"],
            unique=False,
        )


def downgrade():
    if "generation_leases" in sa.inspect(op.get_bind()).get_table_names():
        op.drop_table("generation_leases")
