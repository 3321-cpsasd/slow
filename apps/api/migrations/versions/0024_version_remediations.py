"""Version regenerated remediation content.

Revision ID: 0024_version_remediations
Revises: 0023_correct_demo_persona
"""

from alembic import op
import sqlalchemy as sa


revision = "0024_version_remediations"
down_revision = "0023_correct_demo_persona"
branch_labels = None
depends_on = None

NAMING_CONVENTION = {"uq": "uq_%(table_name)s_%(column_0_name)s"}


def upgrade():
    with op.batch_alter_table(
        "remediations",
        recreate="always",
        naming_convention=NAMING_CONVENTION,
    ) as batch_op:
        batch_op.drop_constraint(
            "uq_remediations_attempt_id",
            type_="unique",
        )
        batch_op.create_index(
            "ix_remediations_attempt_id",
            ["attempt_id"],
            unique=False,
        )
        batch_op.add_column(
            sa.Column("supersedes_id", sa.String(), nullable=True)
        )
        batch_op.create_unique_constraint(
            "uq_remediations_supersedes_id",
            ["supersedes_id"],
        )
        batch_op.create_foreign_key(
            "fk_remediations_supersedes_id",
            "remediations",
            ["supersedes_id"],
            ["id"],
        )


def downgrade():
    connection = op.get_bind()
    duplicates = connection.execute(
        sa.text(
            "SELECT attempt_id FROM remediations GROUP BY attempt_id "
            "HAVING COUNT(*) > 1 LIMIT 1"
        )
    ).first()
    if duplicates:
        raise RuntimeError(
            "Cannot restore one-remediation-per-attempt while revisions exist"
        )
    with op.batch_alter_table(
        "remediations",
        recreate="always",
        naming_convention=NAMING_CONVENTION,
    ) as batch_op:
        batch_op.drop_constraint(
            "fk_remediations_supersedes_id",
            type_="foreignkey",
        )
        batch_op.drop_constraint(
            "uq_remediations_supersedes_id",
            type_="unique",
        )
        batch_op.drop_column("supersedes_id")
        batch_op.drop_index("ix_remediations_attempt_id")
        batch_op.create_unique_constraint(
            "uq_remediations_attempt_id",
            ["attempt_id"],
        )
