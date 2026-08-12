"""Complete learner profile and versioned BKT parameter infrastructure.

Revision ID: 0057_knowledge_engine_completion
Revises: 0056_knowledge_node_ranks
"""

import json

import sqlalchemy as sa
from alembic import op


revision = "0057_knowledge_engine_completion"
down_revision = "0056_knowledge_node_ranks"
branch_labels = None
depends_on = None


DEFAULT_PARAMETERS = {
    "prior_known": 0.2,
    "standard_guess": 0.25,
    "standard_slip": 0.12,
    "assisted_guess": 0.5,
    "oral_partial_guess": 0.48,
    "oral_partial_slip": 0.25,
    "oral_weak_guess": 0.4,
    "oral_weak_slip": 0.4,
}


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade():
    tables = _tables()
    if "learner_knowledge_profile_projections" not in tables:
        op.create_table(
            "learner_knowledge_profile_projections",
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("user_id", sa.String(), nullable=False),
            sa.Column("summary_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("evidence_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("independent_evidence_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("profile_rule_version", sa.String(length=40), nullable=False, server_default="learner_knowledge_profile_v1"),
            sa.Column("projection_version", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("source_observation_watermark", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("rebuilt_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("user_id", name="uq_learner_knowledge_profile_user"),
        )
        op.create_index(
            "ix_learner_knowledge_profile_projections_user_id",
            "learner_knowledge_profile_projections",
            ["user_id"],
        )

    if "bkt_parameter_set_versions" not in tables:
        op.create_table(
            "bkt_parameter_set_versions",
            sa.Column("version", sa.String(length=80), nullable=False),
            sa.Column("scope_kind", sa.String(length=32), nullable=False),
            sa.Column("scope_key", sa.String(length=160), nullable=False),
            sa.Column("parameters_json", sa.Text(), nullable=False),
            sa.Column("training_snapshot_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("evaluation_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("provenance_mode", sa.String(length=40), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("version"),
        )
        for name, columns in (
            ("ix_bkt_parameter_set_versions_scope_kind", ["scope_kind"]),
            ("ix_bkt_parameter_set_versions_scope_key", ["scope_key"]),
            ("ix_bkt_parameter_set_versions_provenance_mode", ["provenance_mode"]),
            ("ix_bkt_parameter_set_versions_created_at", ["created_at"]),
        ):
            op.create_index(name, "bkt_parameter_set_versions", columns)

    if "bkt_parameter_activation_events" not in tables:
        op.create_table(
            "bkt_parameter_activation_events",
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("scope_kind", sa.String(length=32), nullable=False),
            sa.Column("scope_key", sa.String(length=160), nullable=False),
            sa.Column("deployment_mode", sa.String(length=16), nullable=False),
            sa.Column("sequence", sa.Integer(), nullable=False),
            sa.Column("parameter_set_version", sa.String(length=80), nullable=False),
            sa.Column("previous_parameter_set_version", sa.String(length=80), nullable=False, server_default=""),
            sa.Column("decision_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["parameter_set_version"], ["bkt_parameter_set_versions.version"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("scope_kind", "scope_key", "deployment_mode", "sequence", name="uq_bkt_activation_scope_mode_sequence"),
        )
        for name, columns in (
            ("ix_bkt_parameter_activation_events_scope_kind", ["scope_kind"]),
            ("ix_bkt_parameter_activation_events_scope_key", ["scope_key"]),
            ("ix_bkt_parameter_activation_events_deployment_mode", ["deployment_mode"]),
            ("ix_bkt_parameter_activation_events_parameter_set_version", ["parameter_set_version"]),
            ("ix_bkt_parameter_activation_events_created_at", ["created_at"]),
        ):
            op.create_index(name, "bkt_parameter_activation_events", columns)

    connection = op.get_bind()
    exists = connection.execute(
        sa.text("SELECT version FROM bkt_parameter_set_versions WHERE version = 'bkt_multimodal_v2'")
    ).scalar()
    if not exists:
        connection.execute(
            sa.text(
                "INSERT INTO bkt_parameter_set_versions "
                "(version, scope_kind, scope_key, parameters_json, training_snapshot_json, evaluation_json, provenance_mode, created_at) "
                "VALUES ('bkt_multimodal_v2', 'global', '*', :parameters, '{}', :evaluation, 'system_default', CURRENT_TIMESTAMP)"
            ),
            {
                "parameters": json.dumps(DEFAULT_PARAMETERS, sort_keys=True),
                "evaluation": json.dumps({"gatePassed": True, "basis": "frozen_system_baseline"}, sort_keys=True),
            },
        )
        connection.execute(
            sa.text(
                "INSERT INTO bkt_parameter_activation_events "
                "(id, scope_kind, scope_key, deployment_mode, sequence, parameter_set_version, previous_parameter_set_version, decision_json, created_at) "
                "VALUES ('bkt_activation_system_default_v2', 'global', '*', 'online', 1, 'bkt_multimodal_v2', '', :decision, CURRENT_TIMESTAMP)"
            ),
            {"decision": json.dumps({"approved": True, "basis": "migration_seed"}, sort_keys=True)},
        )


def downgrade():
    tables = _tables()
    if "bkt_parameter_activation_events" in tables:
        op.drop_table("bkt_parameter_activation_events")
    if "bkt_parameter_set_versions" in tables:
        op.drop_table("bkt_parameter_set_versions")
    if "learner_knowledge_profile_projections" in tables:
        op.drop_table("learner_knowledge_profile_projections")
