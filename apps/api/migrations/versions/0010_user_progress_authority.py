"""Move user learning and artifact state into per-run projections.

Revision ID: 0010_user_progress_authority
Revises: 0009_learning_workflow_boundaries
"""

from alembic import op
import sqlalchemy as sa


revision = "0010_user_progress_authority"
down_revision = "0009_learning_workflow_boundaries"
branch_labels = None
depends_on = None


def _create_progress_table(
    table_name,
    target_column,
    target_table,
    *,
    section_fields=False,
):
    columns = [
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("learning_run_id", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column(target_column, sa.String(), nullable=False),
        sa.Column(
            "status",
            sa.String(length=24),
            nullable=False,
            server_default="locked",
        ),
    ]
    if section_fields:
        columns.extend(
            [
                sa.Column(
                    "best_score",
                    sa.Integer(),
                    nullable=False,
                    server_default="0",
                ),
                sa.Column(
                    "total_score",
                    sa.Integer(),
                    nullable=False,
                    server_default="0",
                ),
                sa.Column(
                    "ask_me_unlocked",
                    sa.Boolean(),
                    nullable=False,
                    server_default=sa.false(),
                ),
            ]
        )
    columns.extend(
        [
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["learning_run_id"], ["learning_runs.id"]),
            sa.ForeignKeyConstraint([target_column], [f"{target_table}.id"]),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("learning_run_id", target_column),
        ]
    )
    op.create_table(table_name, *columns)
    op.create_index(
        f"ix_{table_name}_learning_run_id",
        table_name,
        ["learning_run_id"],
        unique=False,
    )
    op.create_index(
        f"ix_{table_name}_{target_column}",
        table_name,
        [target_column],
        unique=False,
    )
    op.create_index(
        f"ix_{table_name}_user_id",
        table_name,
        ["user_id"],
        unique=False,
    )


def upgrade():
    connection = op.get_bind()
    inspector = sa.inspect(connection)
    tables = set(inspector.get_table_names())
    if "learning_runs" not in tables:
        op.create_table(
            "learning_runs",
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("user_id", sa.String(), nullable=False),
            sa.Column("series_id", sa.String(), nullable=False),
            sa.Column(
                "status",
                sa.String(length=24),
                nullable=False,
                server_default="active",
            ),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
            sa.ForeignKeyConstraint(["series_id"], ["series.id"]),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            "ix_learning_runs_series_id",
            "learning_runs",
            ["series_id"],
            unique=False,
        )
        op.create_index(
            "ix_learning_runs_status",
            "learning_runs",
            ["status"],
            unique=False,
        )
        op.create_index(
            "ix_learning_runs_user_id",
            "learning_runs",
            ["user_id"],
            unique=False,
        )
    inspector = sa.inspect(connection)
    tables = set(inspector.get_table_names())
    if "book_progress" not in tables:
        _create_progress_table("book_progress", "book_id", "books")
    if "chapter_progress" not in tables:
        _create_progress_table("chapter_progress", "chapter_id", "chapters")
    if "section_progress" not in tables:
        _create_progress_table(
            "section_progress",
            "section_id",
            "sections",
            section_fields=True,
        )
    if "artifact_progress" not in tables:
        op.create_table(
            "artifact_progress",
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("learning_run_id", sa.String(), nullable=False),
            sa.Column("user_id", sa.String(), nullable=False),
            sa.Column("target_type", sa.String(length=32), nullable=False),
            sa.Column("target_id", sa.String(), nullable=False),
            sa.Column(
                "status",
                sa.String(length=24),
                nullable=False,
                server_default="locked",
            ),
            sa.Column(
                "submission_json",
                sa.Text(),
                nullable=False,
                server_default="{}",
            ),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["learning_run_id"], ["learning_runs.id"]),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("learning_run_id", "target_type", "target_id"),
        )
        for column in [
            "learning_run_id",
            "target_id",
            "target_type",
            "user_id",
        ]:
            op.create_index(
                f"ix_artifact_progress_{column}",
                "artifact_progress",
                [column],
                unique=False,
            )

    # The legacy fields are retained only as migration sources. From this
    # revision onward, application writes target the projections below.
    connection.execute(
        sa.text(
            """
            INSERT INTO learning_runs
                (id, user_id, series_id, status, created_at, completed_at)
            SELECT
                'learning_run_migrated_' || s.id,
                sh.user_id,
                s.id,
                'active',
                CURRENT_TIMESTAMP,
                NULL
            FROM series AS s
            JOIN shelves AS sh ON sh.id = s.shelf_id
            WHERE NOT EXISTS (
                SELECT 1 FROM learning_runs AS lr
                WHERE lr.user_id = sh.user_id
                  AND lr.series_id = s.id
                  AND lr.status = 'active'
            )
            """
        )
    )
    connection.execute(
        sa.text(
            """
            INSERT INTO book_progress
                (id, learning_run_id, user_id, book_id, status, updated_at)
            SELECT
                'book_progress_migrated_' || b.id,
                lr.id,
                lr.user_id,
                b.id,
                b.status,
                CURRENT_TIMESTAMP
            FROM books AS b
            JOIN learning_runs AS lr ON lr.series_id = b.series_id
            WHERE lr.status = 'active'
              AND NOT EXISTS (
                SELECT 1 FROM book_progress AS bp
                WHERE bp.learning_run_id = lr.id AND bp.book_id = b.id
              )
            """
        )
    )
    connection.execute(
        sa.text(
            """
            INSERT INTO chapter_progress
                (id, learning_run_id, user_id, chapter_id, status, updated_at)
            SELECT
                'chapter_progress_migrated_' || c.id,
                lr.id,
                lr.user_id,
                c.id,
                c.status,
                CURRENT_TIMESTAMP
            FROM chapters AS c
            JOIN books AS b ON b.id = c.book_id
            JOIN learning_runs AS lr ON lr.series_id = b.series_id
            WHERE lr.status = 'active'
              AND NOT EXISTS (
                SELECT 1 FROM chapter_progress AS cp
                WHERE cp.learning_run_id = lr.id AND cp.chapter_id = c.id
              )
            """
        )
    )
    connection.execute(
        sa.text(
            """
            INSERT INTO section_progress
                (id, learning_run_id, user_id, section_id, status,
                 best_score, total_score, ask_me_unlocked, updated_at)
            SELECT
                'section_progress_migrated_' || sec.id,
                lr.id,
                lr.user_id,
                sec.id,
                sec.status,
                sec.best_score,
                sec.total_score,
                sec.ask_me_unlocked,
                CURRENT_TIMESTAMP
            FROM sections AS sec
            JOIN chapters AS c ON c.id = sec.chapter_id
            JOIN books AS b ON b.id = c.book_id
            JOIN learning_runs AS lr ON lr.series_id = b.series_id
            WHERE lr.status = 'active'
              AND NOT EXISTS (
                SELECT 1 FROM section_progress AS sp
                WHERE sp.learning_run_id = lr.id AND sp.section_id = sec.id
              )
            """
        )
    )
    connection.execute(
        sa.text(
            """
            INSERT INTO artifact_progress
                (id, learning_run_id, user_id, target_type, target_id,
                 status, submission_json, updated_at)
            SELECT
                'artifact_progress_migrated_' || p.id,
                lr.id,
                lr.user_id,
                'chapter_practice',
                p.id,
                p.status,
                p.submission_json,
                CURRENT_TIMESTAMP
            FROM chapter_practices AS p
            JOIN chapters AS c ON c.id = p.chapter_id
            JOIN books AS b ON b.id = c.book_id
            JOIN learning_runs AS lr ON lr.series_id = b.series_id
            WHERE lr.status = 'active'
              AND NOT EXISTS (
                SELECT 1 FROM artifact_progress AS ap
                WHERE ap.learning_run_id = lr.id
                  AND ap.target_type = 'chapter_practice'
                  AND ap.target_id = p.id
              )
            """
        )
    )
    connection.execute(
        sa.text(
            """
            INSERT INTO artifact_progress
                (id, learning_run_id, user_id, target_type, target_id,
                 status, submission_json, updated_at)
            SELECT
                'artifact_progress_migrated_' || c.id,
                lr.id,
                lr.user_id,
                'book_capstone',
                c.id,
                c.status,
                c.submission_json,
                CURRENT_TIMESTAMP
            FROM book_capstones AS c
            JOIN books AS b ON b.id = c.book_id
            JOIN learning_runs AS lr ON lr.series_id = b.series_id
            WHERE lr.status = 'active'
              AND NOT EXISTS (
                SELECT 1 FROM artifact_progress AS ap
                WHERE ap.learning_run_id = lr.id
                  AND ap.target_type = 'book_capstone'
                  AND ap.target_id = c.id
              )
            """
        )
    )


def downgrade():
    for table_name in [
        "artifact_progress",
        "section_progress",
        "chapter_progress",
        "book_progress",
        "learning_runs",
    ]:
        if table_name in sa.inspect(op.get_bind()).get_table_names():
            op.drop_table(table_name)
