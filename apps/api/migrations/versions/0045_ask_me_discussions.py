"""Add resumable, topic-based Ask Me discussions.

Revision ID: 0045_ask_me_discussions
Revises: 0044_chapter_knowledge_identity_scope
"""

from alembic import op
import sqlalchemy as sa


revision = "0045_ask_me_discussions"
down_revision = "0044_chapter_knowledge_identity_scope"
branch_labels = None
depends_on = None


def upgrade():
    required_tables = {
        "ask_me_discussion_sessions",
        "ask_me_discussion_topics",
        "ask_me_discussion_turns",
        "ask_me_discussion_commands",
    }
    existing_tables = set(sa.inspect(op.get_bind()).get_table_names())
    if required_tables.issubset(existing_tables):
        return
    partial = required_tables & existing_tables
    if partial:
        raise RuntimeError(
            "partial Ask Me discussion schema requires explicit repair: "
            + ", ".join(sorted(partial))
        )

    op.create_table(
        "ask_me_discussion_sessions",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("learning_run_id", sa.String(), nullable=False),
        sa.Column("section_id", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("learning_contract_version_id", sa.String(), nullable=True),
        sa.Column("content_version_id", sa.String(), nullable=True),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("active_topic_id", sa.String(), nullable=False),
        sa.Column("pending_turn_id", sa.String(), nullable=False),
        sa.Column("schema_version", sa.String(40), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["learning_run_id", "user_id"],
            ["learning_runs.id", "learning_runs.user_id"],
            name="fk_ask_me_discussion_sessions_run_user",
        ),
        sa.ForeignKeyConstraint(["learning_run_id"], ["learning_runs.id"]),
        sa.ForeignKeyConstraint(["section_id"], ["sections.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(
            ["learning_contract_version_id"],
            ["learning_contract_versions.id"],
        ),
        sa.ForeignKeyConstraint(["content_version_id"], ["content_versions.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "learning_run_id",
            "section_id",
            "user_id",
            name="uq_ask_me_discussion_sessions_run_section_user",
        ),
    )
    for column in (
        "learning_run_id",
        "section_id",
        "user_id",
        "learning_contract_version_id",
        "content_version_id",
        "status",
    ):
        op.create_index(
            f"ix_ask_me_discussion_sessions_{column}",
            "ask_me_discussion_sessions",
            [column],
        )

    op.create_table(
        "ask_me_discussion_topics",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("session_id", sa.String(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(300), nullable=False),
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column("dimension", sa.String(32), nullable=False),
        sa.Column("assessment_target_ids_json", sa.Text(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("current_prompt", sa.Text(), nullable=False),
        sa.Column("turn_count", sa.Integer(), nullable=False),
        sa.Column("evidence_recorded", sa.Boolean(), nullable=False),
        sa.Column("final_assessment_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["session_id"], ["ask_me_discussion_sessions.id"]
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "session_id",
            "position",
            name="uq_ask_me_discussion_topics_position",
        ),
    )
    for column in ("session_id", "dimension", "status"):
        op.create_index(
            f"ix_ask_me_discussion_topics_{column}",
            "ask_me_discussion_topics",
            [column],
        )

    op.create_table(
        "ask_me_discussion_turns",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("session_id", sa.String(), nullable=False),
        sa.Column("topic_id", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("turn_index", sa.Integer(), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("answer", sa.Text(), nullable=False),
        sa.Column("evaluation", sa.String(24), nullable=False),
        sa.Column("feedback_json", sa.Text(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("response_json", sa.Text(), nullable=False),
        sa.Column("error_code", sa.String(80), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["session_id"], ["ask_me_discussion_sessions.id"]
        ),
        sa.ForeignKeyConstraint(["topic_id"], ["ask_me_discussion_topics.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id",
            "idempotency_key",
            name="uq_ask_me_discussion_turns_user_idempotency",
        ),
        sa.UniqueConstraint(
            "topic_id",
            "turn_index",
            name="uq_ask_me_discussion_turns_topic_index",
        ),
    )
    for column in ("session_id", "topic_id", "user_id", "status"):
        op.create_index(
            f"ix_ask_me_discussion_turns_{column}",
            "ask_me_discussion_turns",
            [column],
        )

    op.create_table(
        "ask_me_discussion_commands",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("session_id", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("command_type", sa.String(32), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("response_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["session_id"], ["ask_me_discussion_sessions.id"]
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id",
            "idempotency_key",
            name="uq_ask_me_discussion_commands_user_idempotency",
        ),
    )
    for column in ("session_id", "user_id", "command_type"):
        op.create_index(
            f"ix_ask_me_discussion_commands_{column}",
            "ask_me_discussion_commands",
            [column],
        )


def downgrade():
    op.drop_table("ask_me_discussion_commands")
    op.drop_table("ask_me_discussion_turns")
    op.drop_table("ask_me_discussion_topics")
    op.drop_table("ask_me_discussion_sessions")
