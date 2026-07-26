"""slow v0 clean schema

Revision ID: 0001_slow_v0
Revises:
"""
from alembic import op
from migrations.frozen_schema_v0003 import FrozenBase

revision = "0001_slow_v0"
down_revision = None
branch_labels = None
depends_on = None


BASE_TABLES = [
    "users",
    "shelves",
    "learning_plans",
    "series",
    "books",
    "chapters",
    "sections",
    "content_versions",
    "quiz_sets",
    "quiz_attempts",
    "qa_sessions",
    "qa_messages",
    "learning_notes",
]


def upgrade():
    FrozenBase.metadata.create_all(
        op.get_bind(),
        tables=[FrozenBase.metadata.tables[name] for name in BASE_TABLES],
    )


def downgrade():
    FrozenBase.metadata.drop_all(
        op.get_bind(),
        tables=[
            FrozenBase.metadata.tables[name]
            for name in reversed(BASE_TABLES)
        ],
    )
