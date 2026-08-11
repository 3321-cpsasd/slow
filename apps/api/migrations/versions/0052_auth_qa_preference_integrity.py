"""Harden auth quotas, QA preference lineage, and terminal evidence.

Revision ID: 0052_auth_qa_preference_integrity
Revises: 0051_learning_preference_evidence
"""

from alembic import op
import sqlalchemy as sa


revision = "0052_auth_qa_preference_integrity"
down_revision = "0051_learning_preference_evidence"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    def columns(table_name: str) -> set[str]:
        return {item["name"] for item in inspector.get_columns(table_name)}

    def indexes(table_name: str) -> set[str]:
        return {item["name"] for item in inspector.get_indexes(table_name)}

    credential_columns = columns("local_credentials")
    if "registration_source" not in credential_columns:
        op.add_column(
            "local_credentials",
            sa.Column(
                "registration_source",
                sa.String(length=32),
                nullable=False,
                server_default="unspecified",
            ),
        )
    if "registration_quota_date" not in credential_columns:
        op.add_column(
            "local_credentials",
            sa.Column("registration_quota_date", sa.String(length=10), nullable=True),
        )
    credential_indexes = indexes("local_credentials")
    if "ix_local_credentials_registration_source" not in credential_indexes:
        op.create_index(
            "ix_local_credentials_registration_source",
            "local_credentials",
            ["registration_source"],
        )
    if "ix_local_credentials_registration_quota_date" not in credential_indexes:
        op.create_index(
            "ix_local_credentials_registration_quota_date",
            "local_credentials",
            ["registration_quota_date"],
        )
    if "alpha_registration_quotas" not in inspector.get_table_names():
        op.create_table(
            "alpha_registration_quotas",
            sa.Column("quota_date", sa.String(length=10), nullable=False),
            sa.Column("used_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("limit_snapshot", sa.Integer(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("quota_date"),
        )

    qa_columns = columns("qa_messages")
    for name, type_ in (
        ("preference_request_event_id", sa.String(length=128)),
        ("explanation_style", sa.String(length=32)),
        ("explanation_block_kind", sa.String(length=32)),
    ):
        if name not in qa_columns:
            op.add_column("qa_messages", sa.Column(name, type_, nullable=True))
    if "request_source" not in qa_columns:
        op.add_column(
            "qa_messages",
            sa.Column(
                "request_source",
                sa.String(length=32),
                nullable=False,
                server_default="ask_ai",
            ),
        )
    if "ix_qa_messages_preference_request_event_id" not in indexes("qa_messages"):
        op.create_index(
            "ix_qa_messages_preference_request_event_id",
            "qa_messages",
            ["preference_request_event_id"],
        )

    if "terminal_request_key" not in columns("learning_preference_evidence"):
        op.add_column(
            "learning_preference_evidence",
            sa.Column("terminal_request_key", sa.String(length=128), nullable=True),
        )
    rows = bind.execute(sa.text(
        "SELECT id, user_id, request_event_id "
        "FROM learning_preference_evidence "
        "WHERE request_event_id <> '' "
        "AND signal IN ('helpful', 'unclear', 'adopted') "
        "ORDER BY user_id, request_event_id, occurred_at, id"
    )).mappings()
    latest: dict[tuple[str, str], str] = {}
    for row in rows:
        latest[(row["user_id"], row["request_event_id"])] = row["id"]
    for (_, request_event_id), evidence_id in latest.items():
        bind.execute(
            sa.text(
                "UPDATE learning_preference_evidence "
                "SET terminal_request_key = :request_event_id WHERE id = :id"
            ),
            {"request_event_id": request_event_id, "id": evidence_id},
        )
    if (
        "uq_learning_preference_evidence_user_terminal_request"
        not in indexes("learning_preference_evidence")
    ):
        op.create_index(
            "uq_learning_preference_evidence_user_terminal_request",
            "learning_preference_evidence",
            ["user_id", "terminal_request_key"],
            unique=True,
        )


def downgrade():
    op.drop_index(
        "uq_learning_preference_evidence_user_terminal_request",
        table_name="learning_preference_evidence",
    )
    op.drop_column("learning_preference_evidence", "terminal_request_key")

    op.drop_index(
        "ix_qa_messages_preference_request_event_id",
        table_name="qa_messages",
    )
    for name in (
        "request_source",
        "explanation_block_kind",
        "explanation_style",
        "preference_request_event_id",
    ):
        op.drop_column("qa_messages", name)

    op.drop_table("alpha_registration_quotas")
    op.drop_index(
        "ix_local_credentials_registration_quota_date",
        table_name="local_credentials",
    )
    op.drop_index(
        "ix_local_credentials_registration_source",
        table_name="local_credentials",
    )
    op.drop_column("local_credentials", "registration_quota_date")
    op.drop_column("local_credentials", "registration_source")
