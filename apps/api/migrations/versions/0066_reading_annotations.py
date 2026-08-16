"""Add version-bound reading annotations and versioned Ask AI sessions.

Revision ID: 0066_reading_annotations
Revises: 0065_series_display_title
"""

from alembic import op
import sqlalchemy as sa


revision = "0066_reading_annotations"
down_revision = "0065_series_display_title"
branch_labels = None
depends_on = None


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _columns(table_name: str) -> set[str]:
    return {
        item["name"]
        for item in sa.inspect(op.get_bind()).get_columns(table_name)
    }


def _unique_constraints(table_name: str) -> set[str]:
    return {
        item["name"]
        for item in sa.inspect(op.get_bind()).get_unique_constraints(table_name)
        if item.get("name")
    }


def upgrade():
    if "reading_annotations" not in _tables():
        op.create_table(
            "reading_annotations",
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("learning_run_id", sa.String(), nullable=False),
            sa.Column("user_id", sa.String(), nullable=False),
            sa.Column("section_id", sa.String(), nullable=False),
            sa.Column("content_version_id", sa.String(), nullable=False),
            sa.Column("block_id", sa.String(length=200), nullable=False),
            sa.Column("kind", sa.String(length=24), nullable=False),
            sa.Column("quote_exact", sa.Text(), nullable=False),
            sa.Column("quote_prefix", sa.Text(), nullable=False, server_default=""),
            sa.Column("quote_suffix", sa.Text(), nullable=False, server_default=""),
            sa.Column("start_offset", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("end_offset", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("block_snapshot_hash", sa.String(length=64), nullable=False),
            sa.Column("body", sa.Text(), nullable=False, server_default=""),
            sa.Column("color", sa.String(length=24), nullable=False, server_default="amber"),
            sa.Column("status", sa.String(length=24), nullable=False, server_default="active"),
            sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("idempotency_key", sa.String(length=128), nullable=False),
            sa.Column("request_hash", sa.String(length=64), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(
                ["learning_run_id", "user_id"],
                ["learning_runs.id", "learning_runs.user_id"],
                name="fk_reading_annotations_run_user",
            ),
            sa.ForeignKeyConstraint(["section_id"], ["sections.id"]),
            sa.ForeignKeyConstraint(["content_version_id"], ["content_versions.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "user_id",
                "learning_run_id",
                "idempotency_key",
                name="uq_reading_annotations_user_run_idempotency",
            ),
        )
        for column in (
            "learning_run_id", "user_id", "section_id", "content_version_id",
            "block_id", "kind", "status", "created_at", "updated_at",
        ):
            op.create_index(
                f"ix_reading_annotations_{column}",
                "reading_annotations",
                [column],
            )

    if "reading_annotation_revisions" not in _tables():
        op.create_table(
            "reading_annotation_revisions",
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("annotation_id", sa.String(), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False),
            sa.Column("body", sa.Text(), nullable=False, server_default=""),
            sa.Column("color", sa.String(length=24), nullable=False, server_default="amber"),
            sa.Column("status", sa.String(length=24), nullable=False, server_default="active"),
            sa.Column("source", sa.String(length=32), nullable=False, server_default="user_action"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["annotation_id"], ["reading_annotations.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "annotation_id",
                "version",
                name="uq_reading_annotation_revisions_annotation_version",
            ),
        )
        op.create_index(
            "ix_reading_annotation_revisions_annotation_id",
            "reading_annotation_revisions",
            ["annotation_id"],
        )
        op.create_index(
            "ix_reading_annotation_revisions_created_at",
            "reading_annotation_revisions",
            ["created_at"],
        )

    if "qa_sessions" in _tables():
        constraints = _unique_constraints("qa_sessions")
        with op.batch_alter_table("qa_sessions") as batch:
            if "created_at" not in _columns("qa_sessions"):
                batch.add_column(
                    sa.Column(
                        "created_at",
                        sa.DateTime(timezone=True),
                        nullable=False,
                        server_default=sa.text("CURRENT_TIMESTAMP"),
                    )
                )
                batch.create_index(
                    "ix_qa_sessions_created_at", ["created_at"]
                )
            if "uq_qa_sessions_run_section_user" in constraints:
                batch.drop_constraint(
                    "uq_qa_sessions_run_section_user", type_="unique"
                )
            if "uq_qa_sessions_run_section_user_content" not in constraints:
                batch.create_unique_constraint(
                    "uq_qa_sessions_run_section_user_content",
                    [
                        "learning_run_id",
                        "section_id",
                        "user_id",
                        "content_version_id",
                    ],
                )


def downgrade():
    if "qa_sessions" in _tables():
        constraints = _unique_constraints("qa_sessions")
        with op.batch_alter_table("qa_sessions") as batch:
            if "uq_qa_sessions_run_section_user_content" in constraints:
                batch.drop_constraint(
                    "uq_qa_sessions_run_section_user_content", type_="unique"
                )
            if "uq_qa_sessions_run_section_user" not in constraints:
                batch.create_unique_constraint(
                    "uq_qa_sessions_run_section_user",
                    ["learning_run_id", "section_id", "user_id"],
                )
            if "created_at" in _columns("qa_sessions"):
                batch.drop_index("ix_qa_sessions_created_at")
                batch.drop_column("created_at")
    if "reading_annotation_revisions" in _tables():
        op.drop_table("reading_annotation_revisions")
    if "reading_annotations" in _tables():
        op.drop_table("reading_annotations")
