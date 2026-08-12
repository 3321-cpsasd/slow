"""Add rebuildable user knowledge-node rank projections.

Revision ID: 0055_knowledge_node_ranks
Revises: 0054_m3_pilot_foundations
"""

import hashlib

import sqlalchemy as sa
from alembic import op


revision = "0055_knowledge_node_ranks"
down_revision = "0054_m3_pilot_foundations"
branch_labels = None
depends_on = None


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade():
    if "knowledge_node_state_projections" not in _tables():
        op.create_table(
            "knowledge_node_state_projections",
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("user_id", sa.String(), nullable=False),
            sa.Column("concept_revision_id", sa.String(), nullable=False),
            sa.Column(
                "current_rank",
                sa.String(length=24),
                nullable=False,
                server_default="unranked",
            ),
            sa.Column(
                "current_rank_order", sa.Integer(), nullable=False, server_default="0"
            ),
            sa.Column("current_stars", sa.Integer(), nullable=False, server_default="0"),
            sa.Column(
                "highest_rank",
                sa.String(length=24),
                nullable=False,
                server_default="unranked",
            ),
            sa.Column(
                "highest_rank_order", sa.Integer(), nullable=False, server_default="0"
            ),
            sa.Column("highest_stars", sa.Integer(), nullable=False, server_default="0"),
            sa.Column(
                "activation_state",
                sa.String(length=24),
                nullable=False,
                server_default="learning",
            ),
            sa.Column("stability_days", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("next_due_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_qualified_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("evidence_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column(
                "independent_evidence_count",
                sa.Integer(),
                nullable=False,
                server_default="0",
            ),
            sa.Column(
                "uncertainty_ppm",
                sa.Integer(),
                nullable=False,
                server_default="1000000",
            ),
            sa.Column(
                "rank_rule_version",
                sa.String(length=40),
                nullable=False,
                server_default="knowledge_rank_v2",
            ),
            sa.Column(
                "projection_version", sa.Integer(), nullable=False, server_default="1"
            ),
            sa.Column(
                "source_observation_watermark",
                sa.Integer(),
                nullable=False,
                server_default="0",
            ),
            sa.Column("rebuilt_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
            sa.ForeignKeyConstraint(
                ["concept_revision_id"], ["concept_revisions.id"]
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "user_id",
                "concept_revision_id",
                name="uq_knowledge_node_state_user_concept",
            ),
        )
        for name, columns in (
            ("ix_knowledge_node_state_projections_user_id", ["user_id"]),
            (
                "ix_knowledge_node_state_projections_concept_revision_id",
                ["concept_revision_id"],
            ),
            ("ix_knowledge_node_state_projections_current_rank", ["current_rank"]),
            ("ix_knowledge_node_state_projections_highest_rank", ["highest_rank"]),
            ("ix_knowledge_node_state_projections_activation_state", ["activation_state"]),
            ("ix_knowledge_node_state_projections_next_due_at", ["next_due_at"]),
        ):
            op.create_index(name, "knowledge_node_state_projections", columns)

    # Evidence qualification is append-only.  V3 preserves the selected V2
    # gate/retention decisions, removes repeated quizzes from mastery, and adds
    # the separate rank-progression family without rewriting historical rows.
    connection = op.get_bind()
    observations = connection.execute(
        sa.text(
            "SELECT id, assistance_mode, source_type FROM assessment_observations"
        )
    ).mappings().all()
    for observation in observations:
        existing = connection.execute(
            sa.text(
                "SELECT projection_family, status, reason "
                "FROM evidence_qualification_events "
                "WHERE observation_id = :observation_id "
                "AND rule_version = 'evidence_v2'"
            ),
            {"observation_id": observation["id"]},
        ).mappings().all()
        by_family = {item["projection_family"]: item for item in existing}
        if not by_family:
            continue
        existing_v3 = set(
            connection.execute(
                sa.text(
                    "SELECT projection_family FROM evidence_qualification_events "
                    "WHERE observation_id = :observation_id "
                    "AND rule_version = 'evidence_v3'"
                ),
                {"observation_id": observation["id"]},
            ).scalars()
        )
        fallback = {
            "status": "ineligible",
            "reason": "missing prior qualification",
        }
        mastery = by_family.get("mastery", fallback)
        gate = by_family.get("gate", fallback)
        retention = by_family.get("retention", fallback)
        repeated = observation["assistance_mode"] == "unassisted_repeat"
        assisted = observation["assistance_mode"] == "assisted_immediate"
        oral = observation["source_type"] in {"ask_me", "ask_me_topic"}
        governed = mastery["status"] in {"eligible", "eligible_grouped"}
        rank_status = (
            "eligible_grouped"
            if governed
            and (
                oral
                or observation["assistance_mode"]
                in {"unassisted_initial", "unassisted_review"}
            )
            else "ineligible"
        )
        families = {
            "gate": (gate["status"], gate["reason"]),
            "mastery": (
                "ineligible" if repeated else mastery["status"],
                (
                    "repeated section quiz is practice, not new mastery evidence"
                    if repeated
                    else mastery["reason"]
                ),
            ),
            "retention": (retention["status"], retention["reason"]),
            "rank": (
                "ineligible" if assisted or repeated else rank_status,
                "historical evidence requalified for node-rank projection",
            ),
        }
        for family, (status, reason) in families.items():
            if family in existing_v3:
                continue
            digest = hashlib.sha256(
                f"{observation['id']}:{family}:evidence_v3".encode()
            ).hexdigest()[:32]
            connection.execute(
                sa.text(
                    "INSERT INTO evidence_qualification_events "
                    "(id, observation_id, projection_family, status, reason, "
                    "rule_version, created_at) "
                    "VALUES (:id, :observation_id, :family, :status, :reason, "
                    "'evidence_v3', CURRENT_TIMESTAMP)"
                ),
                {
                    "id": f"qualification_{digest}",
                    "observation_id": observation["id"],
                    "family": family,
                    "status": status,
                    "reason": reason,
                },
            )


def downgrade():
    if "evidence_qualification_events" in _tables():
        op.execute(
            "DELETE FROM evidence_qualification_events "
            "WHERE rule_version = 'evidence_v3'"
        )
    if "knowledge_node_state_projections" in _tables():
        op.drop_table("knowledge_node_state_projections")
