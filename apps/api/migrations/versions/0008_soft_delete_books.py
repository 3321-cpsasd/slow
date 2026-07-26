"""Soft delete books while preserving learning history.

Revision ID: 0008_soft_delete_books
Revises: 0007_plan_creation_idempotency
"""

from alembic import op
import sqlalchemy as sa


revision = "0008_soft_delete_books"
down_revision = "0007_plan_creation_idempotency"
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    columns = {item["name"] for item in inspector.get_columns("books")}
    if "deleted_at" not in columns:
        op.add_column("books", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))
        op.create_index("ix_books_deleted_at", "books", ["deleted_at"], unique=False)


def downgrade():
    inspector = sa.inspect(op.get_bind())
    columns = {item["name"] for item in inspector.get_columns("books")}
    if "deleted_at" in columns:
        indexes = {item["name"] for item in inspector.get_indexes("books")}
        if "ix_books_deleted_at" in indexes:
            op.drop_index("ix_books_deleted_at", table_name="books")
        op.drop_column("books", "deleted_at")
