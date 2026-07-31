"""Remove global learner state and enforce learning-run/user consistency.

Revision ID: 0018_multi_user_state_authority
Revises: 0017_ai_invocation_ledger
"""

from alembic import op
import sqlalchemy as sa


revision = "0018_multi_user_state_authority"
down_revision = "0017_ai_invocation_ledger"
branch_labels = None
depends_on = None


RUN_SCOPED_TABLES = (
    "book_progress",
    "chapter_progress",
    "section_progress",
    "quiz_attempts",
    "qa_sessions",
    "learning_notes",
    "learning_tasks",
    "learning_evidence",
    "ask_me_sessions",
    "artifact_progress",
    "artifact_attachments",
)

LEGACY_STATE_COLUMNS = {
    "books": ("status",),
    "chapters": ("status",),
    "sections": ("status", "best_score", "total_score", "ask_me_unlocked"),
    "chapter_practices": ("status", "submission_json"),
    "book_capstones": ("status", "submission_json"),
}


def _columns(connection, table_name):
    return {
        item["name"]
        for item in sa.inspect(connection).get_columns(table_name)
    }


def _validate_run_user_consistency(connection):
    for table_name in RUN_SCOPED_TABLES:
        mismatch = connection.execute(
            sa.text(
                f"""
                SELECT COUNT(*)
                FROM {table_name} AS scoped
                JOIN learning_runs AS run ON run.id = scoped.learning_run_id
                WHERE scoped.user_id != run.user_id
                """
            )
        ).scalar_one()
        if mismatch:
            raise RuntimeError(
                f"{table_name} contains {mismatch} rows whose user_id "
                "does not match the referenced learning run"
            )


def _recreate_plan_creation_requests(connection):
    connection.execute(
        sa.text(
            """
            CREATE TABLE plan_creation_requests_multi_user (
                idempotency_key VARCHAR(128) NOT NULL,
                user_id VARCHAR NOT NULL,
                request_hash VARCHAR(64) NOT NULL,
                status VARCHAR(24) NOT NULL,
                series_id VARCHAR,
                error_code VARCHAR(80) NOT NULL DEFAULT '',
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL,
                PRIMARY KEY (idempotency_key, user_id),
                FOREIGN KEY(user_id) REFERENCES users (id),
                FOREIGN KEY(series_id) REFERENCES series (id)
            )
            """
        )
    )
    connection.execute(
        sa.text(
            """
            INSERT INTO plan_creation_requests_multi_user (
                idempotency_key, user_id, request_hash, status, series_id,
                error_code, created_at, updated_at
            )
            SELECT
                idempotency_key, user_id, request_hash, status, series_id,
                error_code, created_at, updated_at
            FROM plan_creation_requests
            """
        )
    )
    op.drop_table("plan_creation_requests")
    op.rename_table(
        "plan_creation_requests_multi_user",
        "plan_creation_requests",
    )
    op.create_index(
        "ix_plan_creation_requests_user_id",
        "plan_creation_requests",
        ["user_id"],
        unique=False,
    )
    op.create_index(
        "ix_plan_creation_requests_status",
        "plan_creation_requests",
        ["status"],
        unique=False,
    )


def upgrade():
    connection = op.get_bind()

    user_columns = _columns(connection, "users")
    with op.batch_alter_table("users", recreate="always") as batch_op:
        if "status" not in user_columns:
            batch_op.add_column(
                sa.Column(
                    "status",
                    sa.String(length=24),
                    nullable=False,
                    server_default="active",
                )
            )
        if "created_at" not in user_columns:
            batch_op.add_column(
                sa.Column(
                    "created_at",
                    sa.DateTime(timezone=True),
                    nullable=False,
                    server_default=sa.func.current_timestamp(),
                )
            )
        if "updated_at" not in user_columns:
            batch_op.add_column(
                sa.Column(
                    "updated_at",
                    sa.DateTime(timezone=True),
                    nullable=False,
                    server_default=sa.func.current_timestamp(),
                )
            )
    op.create_index("ix_users_status", "users", ["status"], unique=False)

    _recreate_plan_creation_requests(connection)
    _validate_run_user_consistency(connection)

    op.create_index(
        "uq_learning_runs_id_user",
        "learning_runs",
        ["id", "user_id"],
        unique=True,
    )
    for table_name in RUN_SCOPED_TABLES:
        with op.batch_alter_table(
            table_name,
            recreate="always",
        ) as batch_op:
            batch_op.create_foreign_key(
                f"fk_{table_name}_run_user",
                "learning_runs",
                ["learning_run_id", "user_id"],
                ["id", "user_id"],
            )

    for table_name, legacy_columns in LEGACY_STATE_COLUMNS.items():
        existing = _columns(connection, table_name)
        with op.batch_alter_table(
            table_name,
            recreate="always",
        ) as batch_op:
            for column_name in legacy_columns:
                if column_name in existing:
                    batch_op.drop_column(column_name)


def downgrade():
    connection = op.get_bind()
    for table_name, legacy_columns in LEGACY_STATE_COLUMNS.items():
        existing = _columns(connection, table_name)
        with op.batch_alter_table(
            table_name,
            recreate="always",
        ) as batch_op:
            for column_name in legacy_columns:
                if column_name in existing:
                    continue
                if column_name in {"best_score", "total_score"}:
                    column = sa.Column(
                        column_name,
                        sa.Integer(),
                        nullable=False,
                        server_default="0",
                    )
                elif column_name == "ask_me_unlocked":
                    column = sa.Column(
                        column_name,
                        sa.Boolean(),
                        nullable=False,
                        server_default=sa.false(),
                    )
                elif column_name == "submission_json":
                    column = sa.Column(
                        column_name,
                        sa.Text(),
                        nullable=False,
                        server_default="{}",
                    )
                else:
                    column = sa.Column(
                        column_name,
                        sa.String(length=24),
                        nullable=False,
                        server_default="locked",
                    )
                batch_op.add_column(column)

    for table_name in reversed(RUN_SCOPED_TABLES):
        with op.batch_alter_table(
            table_name,
            recreate="always",
        ) as batch_op:
            batch_op.drop_constraint(
                f"fk_{table_name}_run_user",
                type_="foreignkey",
            )
    op.drop_index("uq_learning_runs_id_user", table_name="learning_runs")

    connection.execute(
        sa.text(
            """
            CREATE TABLE plan_creation_requests_single_user (
                idempotency_key VARCHAR(128) NOT NULL PRIMARY KEY,
                user_id VARCHAR NOT NULL,
                request_hash VARCHAR(64) NOT NULL,
                status VARCHAR(24) NOT NULL,
                series_id VARCHAR,
                error_code VARCHAR(80) NOT NULL DEFAULT '',
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users (id),
                FOREIGN KEY(series_id) REFERENCES series (id)
            )
            """
        )
    )
    connection.execute(
        sa.text(
            """
            INSERT INTO plan_creation_requests_single_user
            SELECT idempotency_key, user_id, request_hash, status, series_id,
                   error_code, created_at, updated_at
            FROM plan_creation_requests
            """
        )
    )
    op.drop_table("plan_creation_requests")
    op.rename_table(
        "plan_creation_requests_single_user",
        "plan_creation_requests",
    )
    op.create_index(
        "ix_plan_creation_requests_user_id",
        "plan_creation_requests",
        ["user_id"],
        unique=False,
    )
    op.create_index(
        "ix_plan_creation_requests_status",
        "plan_creation_requests",
        ["status"],
        unique=False,
    )

    op.drop_index("ix_users_status", table_name="users")
    with op.batch_alter_table("users", recreate="always") as batch_op:
        batch_op.drop_column("updated_at")
        batch_op.drop_column("created_at")
        batch_op.drop_column("status")
