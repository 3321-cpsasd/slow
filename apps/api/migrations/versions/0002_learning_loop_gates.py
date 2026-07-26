"""learning loop hard gates

Revision ID: 0002_learning_loop_gates
Revises: 0001_slow_v0
"""

from alembic import op

from migrations.frozen_schema_v0003 import FrozenBase

revision = "0002_learning_loop_gates"
down_revision = "0001_slow_v0"
branch_labels = None
depends_on = None


NEW_TABLES = [
    "evaluation_runs",
    "chapter_revisions",
    "book_capstones",
    "chapter_practices",
    "ask_me_sessions",
    "learning_memory",
    "learning_evidence",
    "qa_threads",
    "remediations",
    "generation_runs",
    "source_verifications",
]


def upgrade():
    FrozenBase.metadata.create_all(
        op.get_bind(),
        tables=[FrozenBase.metadata.tables[name] for name in NEW_TABLES],
        checkfirst=True,
    )


def downgrade():
    for table_name in NEW_TABLES:
        op.drop_table(table_name)
