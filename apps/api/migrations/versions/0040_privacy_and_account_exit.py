"""Add versioned privacy consent and account exit requests.

Revision ID: 0040_privacy_and_account_exit
Revises: 0039_book_outline_lifecycle
"""

from alembic import op
import sqlalchemy as sa


revision = "0040_privacy_and_account_exit"
down_revision = "0039_book_outline_lifecycle"
branch_labels = None
depends_on = None


def upgrade():
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "privacy_consents" not in tables:
        op.create_table(
            "privacy_consents",
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("user_id", sa.String(), nullable=False),
            sa.Column("notice_version", sa.String(40), nullable=False),
            sa.Column("trial_terms_version", sa.String(40), nullable=False),
            sa.Column("status", sa.String(24), nullable=False),
            sa.Column("source", sa.String(40), nullable=False),
            sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("withdrawn_at", sa.DateTime(timezone=True), nullable=True),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "user_id",
                "notice_version",
                "trial_terms_version",
                name="uq_privacy_consents_user_versions",
            ),
        )
        for column in ("user_id", "notice_version", "trial_terms_version", "status", "accepted_at"):
            op.create_index(op.f(f"ix_privacy_consents_{column}"), "privacy_consents", [column])

    if "account_exit_requests" not in tables:
        op.create_table(
            "account_exit_requests",
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("user_id", sa.String(), nullable=False),
            sa.Column("status", sa.String(24), nullable=False),
            sa.Column("policy_version", sa.String(40), nullable=False),
            sa.Column("reason", sa.Text(), nullable=False),
            sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("deletion_due_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
        for column in ("user_id", "status", "policy_version", "requested_at", "deletion_due_at"):
            op.create_index(op.f(f"ix_account_exit_requests_{column}"), "account_exit_requests", [column])


def downgrade():
    op.drop_table("account_exit_requests")
    op.drop_table("privacy_consents")
