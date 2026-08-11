"""Add versioned recovery codes for Alpha password accounts.

Revision ID: 0049_alpha_account_recovery
Revises: 0048_ask_me_evidence_and_turn_leases
"""

from alembic import op
import sqlalchemy as sa


revision = "0049_alpha_account_recovery"
down_revision = "0048_ask_me_evidence_and_turn_leases"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("account_recovery_codes"):
        op.create_table(
            "account_recovery_codes",
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("user_id", sa.String(), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False),
            sa.Column("code_hash", sa.String(64), nullable=False),
            sa.Column("status", sa.String(24), nullable=False),
            sa.Column("failed_attempts", sa.Integer(), nullable=False),
            sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "user_id",
                "version",
                name="uq_account_recovery_codes_user_version",
            ),
        )
    inspector = sa.inspect(bind)
    existing_indexes = {
        item["name"]
        for item in inspector.get_indexes("account_recovery_codes")
    }
    for name, columns, unique in (
        ("ix_account_recovery_codes_user_id", ["user_id"], False),
        ("ix_account_recovery_codes_code_hash", ["code_hash"], True),
        ("ix_account_recovery_codes_status", ["status"], False),
        ("ix_account_recovery_codes_locked_until", ["locked_until"], False),
    ):
        if name not in existing_indexes:
            op.create_index(
                name,
                "account_recovery_codes",
                columns,
                unique=unique,
            )


def downgrade():
    existing = op.get_bind().execute(sa.text(
        "SELECT id FROM account_recovery_codes LIMIT 1"
    )).first()
    if existing:
        raise RuntimeError(
            "0049 downgrade refused: account recovery history would be lost"
        )
    op.drop_table("account_recovery_codes")
