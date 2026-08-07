"""Bind quizzes to content versions and repair fresh-install uniqueness.

Revision ID: 0012_content_lineage
Revises: 0011_learning_run_fact_scope
"""

from alembic import op
import sqlalchemy as sa


revision = "0012_content_lineage"
down_revision = "0011_learning_run_fact_scope"
branch_labels = None
depends_on = None

NAMING_CONVENTION = {"uq": "uq_%(table_name)s_%(column_0_name)s"}


def _columns(connection, table_name):
    return {
        item["name"]
        for item in sa.inspect(connection).get_columns(table_name)
    }


def _unique_names(connection, table_name):
    return {
        item["name"]
        for item in sa.inspect(connection).get_unique_constraints(table_name)
    }


def upgrade():
    connection = op.get_bind()

    # Older fresh installs could inherit both the run-scoped constraint and
    # the superseded user-global constraint because the original baseline
    # imported live ORM metadata.
    legacy_constraint = "uq_quiz_attempts_user_id_idempotency_key"
    if legacy_constraint in _unique_names(connection, "quiz_attempts"):
        with op.batch_alter_table(
            "quiz_attempts",
            recreate="always",
            naming_convention=NAMING_CONVENTION,
        ) as batch_op:
            batch_op.drop_constraint(legacy_constraint, type_="unique")

    if "content_version_id" not in _columns(connection, "quiz_sets"):
        op.add_column(
            "quiz_sets",
            sa.Column("content_version_id", sa.String(), nullable=True),
        )
        op.create_index(
            "ix_quiz_sets_content_version_id",
            "quiz_sets",
            ["content_version_id"],
            unique=False,
        )

    # A legacy quiz did not record its exact content version. The latest
    # version available for the same section is the only defensible backfill;
    # every newly generated quiz records the exact foreign key at write time.
    connection.execute(
        sa.text(
            """
            UPDATE quiz_sets
            SET content_version_id = (
                SELECT cv.id
                FROM content_versions AS cv
                WHERE cv.section_id = quiz_sets.section_id
                ORDER BY cv.version DESC
                LIMIT 1
            )
            WHERE content_version_id IS NULL
            """
        )
    )
    missing = connection.execute(
        sa.text(
            "SELECT COUNT(*) FROM quiz_sets "
            "WHERE content_version_id IS NULL"
        )
    ).scalar_one()
    if missing:
        raise RuntimeError(
            f"quiz_sets has {missing} rows without a content version"
        )
    quiz_columns = {
        item["name"]: item
        for item in sa.inspect(connection).get_columns("quiz_sets")
    }
    quiz_foreign_keys = {
        tuple(item["constrained_columns"])
        for item in sa.inspect(connection).get_foreign_keys("quiz_sets")
    }
    if (
        quiz_columns["content_version_id"]["nullable"]
        or ("content_version_id",) not in quiz_foreign_keys
    ):
        with op.batch_alter_table("quiz_sets") as batch_op:
            batch_op.alter_column(
                "content_version_id",
                existing_type=sa.String(),
                nullable=False,
            )
            if ("content_version_id",) not in quiz_foreign_keys:
                batch_op.create_foreign_key(
                    "fk_quiz_sets_content_version_id",
                    "content_versions",
                    ["content_version_id"],
                    ["id"],
                )

    content_unique = "uq_content_versions_section_version"
    if content_unique not in _unique_names(connection, "content_versions"):
        if connection.dialect.name == "postgresql":
            op.create_unique_constraint(
                content_unique,
                "content_versions",
                ["section_id", "version"],
            )
            return
        with op.batch_alter_table(
            "content_versions",
            recreate="always",
            naming_convention=NAMING_CONVENTION,
        ) as batch_op:
            batch_op.create_unique_constraint(
                content_unique,
                ["section_id", "version"],
            )


def downgrade():
    connection = op.get_bind()
    if "content_version_id" in _columns(connection, "quiz_sets"):
        with op.batch_alter_table("quiz_sets") as batch_op:
            batch_op.drop_column("content_version_id")
    if (
        "uq_content_versions_section_version"
        in _unique_names(connection, "content_versions")
    ):
        with op.batch_alter_table("content_versions") as batch_op:
            batch_op.drop_constraint(
                "uq_content_versions_section_version",
                type_="unique",
            )
