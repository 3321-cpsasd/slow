"""Persist artifact submissions as immutable facts.

Revision ID: 0021_artifact_submission_facts
Revises: 0020_worker_fencing_and_resume
"""

import json

from alembic import op
import sqlalchemy as sa


revision = "0021_artifact_submission_facts"
down_revision = "0020_worker_fencing_and_resume"
branch_labels = None
depends_on = None


def upgrade():
    connection = op.get_bind()
    if not sa.inspect(connection).has_table("artifact_submissions"):
        op.create_table(
            "artifact_submissions",
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("learning_run_id", sa.String(), nullable=False),
            sa.Column("user_id", sa.String(), nullable=False),
            sa.Column("target_type", sa.String(length=32), nullable=False),
            sa.Column("target_id", sa.String(), nullable=False),
            sa.Column("content_json", sa.Text(), nullable=False),
            sa.Column(
                "attachment_ids_json",
                sa.Text(),
                nullable=False,
                server_default="[]",
            ),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["learning_run_id"], ["learning_runs.id"]),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
            sa.ForeignKeyConstraint(
                ["learning_run_id", "user_id"],
                ["learning_runs.id", "learning_runs.user_id"],
                name="fk_artifact_submissions_run_user",
            ),
            sa.PrimaryKeyConstraint("id"),
        )
    submission_indexes = {
        item["name"]
        for item in sa.inspect(connection).get_indexes("artifact_submissions")
    }
    for column_name in (
        "learning_run_id",
        "user_id",
        "target_type",
        "target_id",
        "created_at",
    ):
        index_name = f"ix_artifact_submissions_{column_name}"
        if index_name not in submission_indexes:
            op.create_index(
                index_name,
                "artifact_submissions",
                [column_name],
                unique=False,
            )

    rows = connection.execute(
        sa.text(
            """
            SELECT id, learning_run_id, user_id, target_type, target_id,
                   submission_json, updated_at
            FROM artifact_progress
            WHERE submission_json != '{}'
            """
        )
    ).mappings()
    for row in rows:
        submission_id = f"artifact_submission_migrated_{row['id']}"
        if connection.execute(
            sa.text(
                "SELECT 1 FROM artifact_submissions WHERE id = :id"
            ),
            {"id": submission_id},
        ).first():
            continue
        try:
            payload = json.loads(row["submission_json"] or "{}")
        except json.JSONDecodeError:
            payload = {}
        connection.execute(
            sa.text(
                """
                INSERT INTO artifact_submissions (
                    id, learning_run_id, user_id, target_type, target_id,
                    content_json, attachment_ids_json, created_at
                ) VALUES (
                    :id, :learning_run_id, :user_id, :target_type, :target_id,
                    :content_json, :attachment_ids_json, :created_at
                )
                """
            ),
            {
                "id": submission_id,
                "learning_run_id": row["learning_run_id"],
                "user_id": row["user_id"],
                "target_type": row["target_type"],
                "target_id": row["target_id"],
                "content_json": json.dumps(
                    payload.get("content", {}),
                    ensure_ascii=False,
                ),
                "attachment_ids_json": json.dumps(
                    payload.get("attachmentIds", []),
                    ensure_ascii=False,
                ),
                "created_at": row["updated_at"],
            },
        )


def downgrade():
    for column_name in reversed(
        (
            "learning_run_id",
            "user_id",
            "target_type",
            "target_id",
            "created_at",
        )
    ):
        op.drop_index(
            f"ix_artifact_submissions_{column_name}",
            table_name="artifact_submissions",
        )
    op.drop_table("artifact_submissions")
