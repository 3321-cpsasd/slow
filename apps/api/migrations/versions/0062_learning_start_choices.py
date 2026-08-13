"""Add learning-start preferences and chapter route choices.

Revision ID: 0062_learning_start_choices
Revises: 0061_ask_me_role_lineage
"""

from alembic import op
import sqlalchemy as sa


revision = "0062_learning_start_choices"
down_revision = "0061_ask_me_role_lineage"
branch_labels = None
depends_on = None


def upgrade():
    # Older installations can have the current ORM metadata created ahead of
    # their Alembic revision. In that recovery path all four tables (and their
    # ORM-declared indexes) already exist, so the migration only needs to
    # advance the revision instead of trying to recreate them.
    expected_tables = {
        "learning_start_previews",
        "series_learning_start_preferences",
        "chapter_route_decision_events",
        "chapter_challenge_attempts",
    }
    existing_tables = set(sa.inspect(op.get_bind()).get_table_names())
    if expected_tables.issubset(existing_tables):
        return

    op.create_table(
        "learning_start_previews",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("shelf_id", sa.String(), nullable=False),
        sa.Column("knowledge_graph_release_id", sa.String(), nullable=True),
        sa.Column("topic", sa.String(length=160), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "visible_concept_revision_ids_json",
            sa.Text(),
            nullable=False,
            server_default="[]",
        ),
        sa.Column("snapshot_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column(
            "schema_version",
            sa.String(length=48),
            nullable=False,
            server_default="learning_start_preview_v1",
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["shelf_id"], ["shelves.id"]),
        sa.ForeignKeyConstraint(
            ["knowledge_graph_release_id"], ["knowledge_graph_releases.id"]
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_learning_start_previews_user_id",
        "learning_start_previews",
        ["user_id"],
    )
    op.create_index(
        "ix_learning_start_previews_shelf_id",
        "learning_start_previews",
        ["shelf_id"],
    )
    op.create_index(
        "ix_learning_start_previews_release_id",
        "learning_start_previews",
        ["knowledge_graph_release_id"],
    )
    op.create_index(
        "ix_learning_start_previews_request_hash",
        "learning_start_previews",
        ["request_hash"],
    )

    op.create_table(
        "series_learning_start_preferences",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("series_id", sa.String(), nullable=False),
        sa.Column("preview_id", sa.String(), nullable=True),
        sa.Column("start_mode", sa.String(length=24), nullable=False),
        sa.Column(
            "selected_concept_revision_ids_json",
            sa.Text(),
            nullable=False,
            server_default="[]",
        ),
        sa.Column(
            "learning_preferences_json",
            sa.Text(),
            nullable=False,
            server_default="[]",
        ),
        sa.Column(
            "rule_version",
            sa.String(length=48),
            nullable=False,
            server_default="learning_start_selection_v1",
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["series_id"], ["series.id"]),
        sa.ForeignKeyConstraint(["preview_id"], ["learning_start_previews.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "series_id", name="uq_series_learning_start_preference"
        ),
    )
    op.create_index(
        "ix_series_learning_start_preferences_user_id",
        "series_learning_start_preferences",
        ["user_id"],
    )
    op.create_index(
        "ix_series_learning_start_preferences_series_id",
        "series_learning_start_preferences",
        ["series_id"],
    )
    op.create_index(
        "ix_series_learning_start_preferences_preview_id",
        "series_learning_start_preferences",
        ["preview_id"],
    )
    op.create_index(
        "ix_series_learning_start_preferences_start_mode",
        "series_learning_start_preferences",
        ["start_mode"],
    )

    op.create_table(
        "chapter_route_decision_events",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("learning_run_id", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("chapter_id", sa.String(), nullable=False),
        sa.Column("action", sa.String(length=24), nullable=False),
        sa.Column("reason", sa.String(length=32), nullable=False, server_default=""),
        sa.Column(
            "source",
            sa.String(length=32),
            nullable=False,
            server_default="chapter_entry",
        ),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("outcome_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column(
            "rule_version",
            sa.String(length=48),
            nullable=False,
            server_default="chapter_route_choice_v1",
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["learning_run_id", "user_id"],
            ["learning_runs.id", "learning_runs.user_id"],
            name="fk_chapter_route_decision_run_user",
        ),
        sa.ForeignKeyConstraint(["chapter_id"], ["chapters.id"]),
        sa.ForeignKeyConstraint(["learning_run_id"], ["learning_runs.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "learning_run_id",
            "user_id",
            "idempotency_key",
            name="uq_chapter_route_decision_run_user_idempotency",
        ),
    )
    for column in ("learning_run_id", "user_id", "chapter_id", "action", "reason"):
        op.create_index(
            f"ix_chapter_route_decision_events_{column}",
            "chapter_route_decision_events",
            [column],
        )

    op.create_table(
        "chapter_challenge_attempts",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("learning_run_id", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("chapter_id", sa.String(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "status",
            sa.String(length=24),
            nullable=False,
            server_default="processing",
        ),
        sa.Column("passed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("request_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("response_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column(
            "rule_version",
            sa.String(length=48),
            nullable=False,
            server_default="chapter_challenge_v1",
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["learning_run_id", "user_id"],
            ["learning_runs.id", "learning_runs.user_id"],
            name="fk_chapter_challenge_run_user",
        ),
        sa.ForeignKeyConstraint(["chapter_id"], ["chapters.id"]),
        sa.ForeignKeyConstraint(["learning_run_id"], ["learning_runs.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "learning_run_id",
            "user_id",
            "idempotency_key",
            name="uq_chapter_challenge_run_user_idempotency",
        ),
    )
    for column in ("learning_run_id", "user_id", "chapter_id", "status"):
        op.create_index(
            f"ix_chapter_challenge_attempts_{column}",
            "chapter_challenge_attempts",
            [column],
        )


def downgrade():
    op.drop_table("chapter_challenge_attempts")
    op.drop_table("chapter_route_decision_events")
    op.drop_table("series_learning_start_preferences")
    op.drop_table("learning_start_previews")
