"""Make Ask Me evidence authoritative and discussion turns recoverable.

Revision ID: 0048_ask_me_evidence_and_turn_leases
Revises: 0047_historical_schema_repair
"""

from alembic import op
import sqlalchemy as sa


revision = "0048_ask_me_evidence_and_turn_leases"
down_revision = "0047_historical_schema_repair"
branch_labels = None
depends_on = None


def _columns(inspector, table_name: str) -> dict[str, dict]:
    return {item["name"]: item for item in inspector.get_columns(table_name)}


def _unique_names(inspector, table_name: str) -> set[str]:
    return {
        str(item["name"])
        for item in inspector.get_unique_constraints(table_name)
        if item.get("name")
    }


def _index_names(inspector, table_name: str) -> set[str]:
    return {
        str(item["name"])
        for item in inspector.get_indexes(table_name)
        if item.get("name")
    }


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    observation_columns = _columns(inspector, "assessment_observations")
    observation_uniques = _unique_names(inspector, "assessment_observations")
    with op.batch_alter_table("assessment_observations") as batch:
        if "source_type" not in observation_columns:
            batch.add_column(sa.Column(
                "source_type",
                sa.String(32),
                nullable=False,
                server_default="choice_quiz",
            ))
        if "evidence_key" not in observation_columns:
            batch.add_column(sa.Column(
                "evidence_key",
                sa.String(96),
                nullable=True,
            ))
        for name, column_type in (
            ("attempt_id", sa.String()),
            ("scoring_result_id", sa.String()),
            ("question_index", sa.Integer()),
        ):
            if not observation_columns[name]["nullable"]:
                batch.alter_column(
                    name,
                    existing_type=column_type,
                    nullable=True,
                )
        if (
            "uq_assessment_observations_evidence_key"
            not in observation_uniques
        ):
            batch.create_unique_constraint(
                "uq_assessment_observations_evidence_key",
                ["evidence_key"],
            )

    inspector = sa.inspect(bind)
    if (
        "ix_assessment_observations_source_type"
        not in _index_names(inspector, "assessment_observations")
    ):
        op.create_index(
            "ix_assessment_observations_source_type",
            "assessment_observations",
            ["source_type"],
        )

    turn_columns = _columns(inspector, "ask_me_discussion_turns")
    turn_uniques = _unique_names(inspector, "ask_me_discussion_turns")
    with op.batch_alter_table("ask_me_discussion_turns") as batch:
        if "uq_ask_me_discussion_turns_topic_index" in turn_uniques:
            batch.drop_constraint(
                "uq_ask_me_discussion_turns_topic_index",
                type_="unique",
            )
        if "lease_token" not in turn_columns:
            batch.add_column(sa.Column(
                "lease_token",
                sa.String(160),
                nullable=False,
                server_default="",
            ))
        if "lease_expires_at" not in turn_columns:
            batch.add_column(sa.Column(
                "lease_expires_at",
                sa.DateTime(timezone=True),
                nullable=True,
            ))

    inspector = sa.inspect(bind)
    if (
        "ix_ask_me_discussion_turns_lease_expires_at"
        not in _index_names(inspector, "ask_me_discussion_turns")
    ):
        op.create_index(
            "ix_ask_me_discussion_turns_lease_expires_at",
            "ask_me_discussion_turns",
            ["lease_expires_at"],
        )

    # Rows left in processing by an older worker have no fencing token and can
    # never safely complete. Fail them closed and release their sessions.
    op.execute(sa.text(
        "UPDATE ask_me_discussion_turns "
        "SET status = 'failed', "
        "error_code = 'ASK_ME_DISCUSSION_TURN_LEASE_EXPIRED', "
        "lease_token = '', lease_expires_at = NULL "
        "WHERE status = 'processing'"
    ))
    op.execute(sa.text(
        "UPDATE ask_me_discussion_sessions SET pending_turn_id = '' "
        "WHERE pending_turn_id IN ("
        "SELECT id FROM ask_me_discussion_turns "
        "WHERE status = 'failed' "
        "AND error_code = 'ASK_ME_DISCUSSION_TURN_LEASE_EXPIRED'"
        ")"
    ))


def downgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    oral_observation = bind.execute(sa.text(
        "SELECT id FROM assessment_observations "
        "WHERE source_type <> 'choice_quiz' OR evidence_key IS NOT NULL "
        "LIMIT 1"
    )).first()
    if oral_observation:
        raise RuntimeError(
            "0048 downgrade refused: oral assessment facts cannot be "
            "represented by revision 0047"
        )

    duplicate_turn = bind.execute(sa.text(
        "SELECT topic_id, turn_index FROM ask_me_discussion_turns "
        "GROUP BY topic_id, turn_index HAVING COUNT(*) > 1 LIMIT 1"
    )).first()
    if duplicate_turn:
        raise RuntimeError(
            "0048 downgrade refused: discussion retry history violates the "
            "revision 0047 topic/turn uniqueness constraint"
        )

    active_lease = bind.execute(sa.text(
        "SELECT id FROM ask_me_discussion_turns "
        "WHERE status = 'processing' OR lease_token <> '' "
        "OR lease_expires_at IS NOT NULL LIMIT 1"
    )).first()
    if active_lease:
        raise RuntimeError(
            "0048 downgrade refused: an active discussion turn lease would "
            "lose its fencing state"
        )

    if (
        "ix_ask_me_discussion_turns_lease_expires_at"
        in _index_names(inspector, "ask_me_discussion_turns")
    ):
        op.drop_index(
            "ix_ask_me_discussion_turns_lease_expires_at",
            table_name="ask_me_discussion_turns",
        )
    with op.batch_alter_table("ask_me_discussion_turns") as batch:
        batch.drop_column("lease_expires_at")
        batch.drop_column("lease_token")
        batch.create_unique_constraint(
            "uq_ask_me_discussion_turns_topic_index",
            ["topic_id", "turn_index"],
        )

    inspector = sa.inspect(bind)
    if (
        "ix_assessment_observations_source_type"
        in _index_names(inspector, "assessment_observations")
    ):
        op.drop_index(
            "ix_assessment_observations_source_type",
            table_name="assessment_observations",
        )
    with op.batch_alter_table("assessment_observations") as batch:
        batch.drop_constraint(
            "uq_assessment_observations_evidence_key",
            type_="unique",
        )
        batch.alter_column(
            "attempt_id",
            existing_type=sa.String(),
            nullable=False,
        )
        batch.alter_column(
            "scoring_result_id",
            existing_type=sa.String(),
            nullable=False,
        )
        batch.alter_column(
            "question_index",
            existing_type=sa.Integer(),
            nullable=False,
        )
        batch.drop_column("evidence_key")
        batch.drop_column("source_type")
