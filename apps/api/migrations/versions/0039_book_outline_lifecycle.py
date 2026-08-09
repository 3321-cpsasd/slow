"""Add explicit book chapter-outline lifecycle.

Revision ID: 0039_book_outline_lifecycle
Revises: 0038_lesson_generation_v2
"""

from alembic import op
import sqlalchemy as sa


revision = "0039_book_outline_lifecycle"
down_revision = "0038_lesson_generation_v2"
branch_labels = None
depends_on = None


def _column_names(table_name: str) -> set[str]:
    return {
        item["name"]
        for item in sa.inspect(op.get_bind()).get_columns(table_name)
    }


def upgrade():
    columns = _column_names("books")
    if "outline_status" not in columns:
        op.add_column(
            "books",
            sa.Column(
                "outline_status",
                sa.String(24),
                nullable=False,
                server_default="confirmed",
            ),
        )
    if "outline_version" not in columns:
        op.add_column(
            "books",
            sa.Column(
                "outline_version",
                sa.Integer(),
                nullable=False,
                server_default="1",
            ),
        )
    if "outline_confirmed_at" not in columns:
        op.add_column(
            "books",
            sa.Column("outline_confirmed_at", sa.DateTime(timezone=True), nullable=True),
        )
    indexes = {
        item["name"]
        for item in sa.inspect(op.get_bind()).get_indexes("books")
    }
    if "ix_books_outline_status" not in indexes:
        op.create_index("ix_books_outline_status", "books", ["outline_status"])


def downgrade():
    indexes = {
        item["name"]
        for item in sa.inspect(op.get_bind()).get_indexes("books")
    }
    if "ix_books_outline_status" in indexes:
        op.drop_index("ix_books_outline_status", table_name="books")
    columns = _column_names("books")
    for name in ("outline_confirmed_at", "outline_version", "outline_status"):
        if name in columns:
            op.drop_column("books", name)
