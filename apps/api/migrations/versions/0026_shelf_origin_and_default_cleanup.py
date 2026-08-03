"""Separate demo shelves from user-created library state.

Revision ID: 0026_shelf_origin_cleanup
Revises: 0025_profile_onboarding
"""

from alembic import op
import sqlalchemy as sa


revision = "0026_shelf_origin_cleanup"
down_revision = "0025_profile_onboarding"
branch_labels = None
depends_on = None


DEMO_USER_IDS = (
    "user_demo",
    "user_cs_freshman",
    "user_finance_postgrad",
    "user_math_functional",
    "user_dance_civil",
    "user_fashion_to_ux",
)


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("shelves")}
    if "origin" not in columns:
        op.add_column(
            "shelves",
            sa.Column(
                "origin",
                sa.String(32),
                nullable=False,
                server_default="user_created",
            ),
        )

    indexes = {index["name"] for index in sa.inspect(bind).get_indexes("shelves")}
    if "ix_shelves_origin" not in indexes:
        op.create_index("ix_shelves_origin", "shelves", ["origin"], unique=False)

    demo_placeholders = ", ".join(f":demo_{index}" for index, _ in enumerate(DEMO_USER_IDS))
    demo_parameters = {
        f"demo_{index}": user_id
        for index, user_id in enumerate(DEMO_USER_IDS)
    }
    bind.execute(
        sa.text(
            f"UPDATE shelves SET origin='demo_seed' "
            f"WHERE user_id IN ({demo_placeholders})"
        ),
        demo_parameters,
    )

    # This exact signature was written by the old non-demo login seed path.
    # Mark it before cleanup so retained rows remain auditable.
    bind.execute(
        sa.text(
            """
            UPDATE shelves
            SET origin='legacy_auto_seed'
            WHERE origin='user_created'
              AND name='技术'
              AND domain='计算机'
              AND specialty='软件工程'
              AND tags_json IN ('["AI","云原生"]', '["AI", "云原生"]')
            """
        )
    )

    # Only delete the old generated shell when it has no dependent learning
    # facts. Non-empty rows are retained with origin=legacy_auto_seed.
    bind.execute(
        sa.text(
            """
            DELETE FROM shelves
            WHERE origin='legacy_auto_seed'
              AND NOT EXISTS (
                  SELECT 1 FROM learning_plans p WHERE p.shelf_id=shelves.id
              )
              AND NOT EXISTS (
                  SELECT 1 FROM series s WHERE s.shelf_id=shelves.id
              )
              AND NOT EXISTS (
                  SELECT 1 FROM books b WHERE b.shelf_id=shelves.id
              )
              AND NOT EXISTS (
                  SELECT 1 FROM learning_evidence e WHERE e.shelf_id=shelves.id
              )
              AND NOT EXISTS (
                  SELECT 1 FROM learning_memory m WHERE m.shelf_id=shelves.id
              )
            """
        )
    )


def downgrade():
    # Deleted empty generated shelves intentionally stay deleted; recreating
    # them would reintroduce the product defect.
    indexes = {
        index["name"]
        for index in sa.inspect(op.get_bind()).get_indexes("shelves")
    }
    if "ix_shelves_origin" in indexes:
        op.drop_index("ix_shelves_origin", table_name="shelves")
    columns = {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns("shelves")
    }
    if "origin" in columns:
        op.drop_column("shelves", "origin")
