"""Add reusable knowledge networks and frozen capability subnets.

Revision ID: 0068_capability_knowledge_subnets
Revises: 0067_capability_application_tasks
"""

from alembic import op
import sqlalchemy as sa


revision = "0068_capability_knowledge_subnets"
down_revision = "0067_capability_application_tasks"
branch_labels = None
depends_on = None


_ALIASES = {
    "knowledge_networks": "kn",
    "knowledge_network_revisions": "kn_rev",
    "knowledge_network_concept_bindings": "kn_concept",
    "knowledge_relations": "kr",
    "knowledge_relation_revisions": "kr_rev",
    "knowledge_network_relation_bindings": "kn_relation",
    "capability_subnets": "cap_subnet",
    "capability_relation_requirements": "cap_relation",
    "assessment_target_concept_bindings": "target_concept",
    "assessment_target_relation_bindings": "target_relation",
}


def _index(table: str, column: str) -> None:
    name = f"ix_{_ALIASES[table]}_{column}"
    if len(name) > 63:
        raise ValueError(f"PostgreSQL index identifier is too long: {name}")
    op.create_index(name, table, [column])


def upgrade():
    tables = set(sa.inspect(op.get_bind()).get_table_names())

    if "knowledge_networks" not in tables:
        op.create_table(
            "knowledge_networks",
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("namespace", sa.String(length=120), nullable=False),
            sa.Column("network_key", sa.String(length=200), nullable=False),
            sa.Column("label", sa.String(length=300), nullable=False),
            sa.Column("status", sa.String(length=24), nullable=False),
            sa.Column("origin", sa.String(length=40), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "namespace", "network_key", name="uq_knowledge_network_namespace_key"
            ),
        )
        _index("knowledge_networks", "namespace")
        _index("knowledge_networks", "status")

    if "knowledge_network_revisions" not in tables:
        op.create_table(
            "knowledge_network_revisions",
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("knowledge_network_id", sa.String(), nullable=False),
            sa.Column("revision", sa.Integer(), nullable=False),
            sa.Column("status", sa.String(length=24), nullable=False),
            sa.Column("provenance_mode", sa.String(length=40), nullable=False),
            sa.Column("source_release_ids_json", sa.Text(), nullable=False),
            sa.Column("boundary_json", sa.Text(), nullable=False),
            sa.Column("content_hash", sa.String(length=64), nullable=False),
            sa.Column("supersedes_id", sa.String(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["knowledge_network_id"], ["knowledge_networks.id"]),
            sa.ForeignKeyConstraint(["supersedes_id"], ["knowledge_network_revisions.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("content_hash"),
            sa.UniqueConstraint(
                "knowledge_network_id",
                "revision",
                name="uq_knowledge_network_revision_identity",
            ),
        )
        for column in ("knowledge_network_id", "status", "content_hash"):
            _index("knowledge_network_revisions", column)

    if "knowledge_network_concept_bindings" not in tables:
        op.create_table(
            "knowledge_network_concept_bindings",
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("knowledge_network_revision_id", sa.String(), nullable=False),
            sa.Column("concept_revision_id", sa.String(), nullable=False),
            sa.Column("position", sa.Integer(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(
                ["knowledge_network_revision_id"], ["knowledge_network_revisions.id"]
            ),
            sa.ForeignKeyConstraint(["concept_revision_id"], ["concept_revisions.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "knowledge_network_revision_id",
                "concept_revision_id",
                name="uq_knowledge_network_concept_identity",
            ),
        )
        _index("knowledge_network_concept_bindings", "knowledge_network_revision_id")
        _index("knowledge_network_concept_bindings", "concept_revision_id")

    if "knowledge_relations" not in tables:
        op.create_table(
            "knowledge_relations",
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("namespace", sa.String(length=120), nullable=False),
            sa.Column("relation_key", sa.String(length=200), nullable=False),
            sa.Column("status", sa.String(length=24), nullable=False),
            sa.Column("origin", sa.String(length=40), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "namespace", "relation_key", name="uq_knowledge_relation_namespace_key"
            ),
        )
        _index("knowledge_relations", "namespace")
        _index("knowledge_relations", "status")

    if "knowledge_relation_revisions" not in tables:
        op.create_table(
            "knowledge_relation_revisions",
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("knowledge_relation_id", sa.String(), nullable=False),
            sa.Column("revision", sa.Integer(), nullable=False),
            sa.Column("from_concept_revision_id", sa.String(), nullable=False),
            sa.Column("to_concept_revision_id", sa.String(), nullable=False),
            sa.Column("relation_type", sa.String(length=40), nullable=False),
            sa.Column("statement", sa.Text(), nullable=False),
            sa.Column("scope_json", sa.Text(), nullable=False),
            sa.Column("provenance_json", sa.Text(), nullable=False),
            sa.Column("verification_status", sa.String(length=32), nullable=False),
            sa.Column("supersedes_id", sa.String(), nullable=True),
            sa.Column("content_hash", sa.String(length=64), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["knowledge_relation_id"], ["knowledge_relations.id"]),
            sa.ForeignKeyConstraint(["from_concept_revision_id"], ["concept_revisions.id"]),
            sa.ForeignKeyConstraint(["to_concept_revision_id"], ["concept_revisions.id"]),
            sa.ForeignKeyConstraint(["supersedes_id"], ["knowledge_relation_revisions.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("content_hash"),
            sa.UniqueConstraint(
                "knowledge_relation_id",
                "revision",
                name="uq_knowledge_relation_revision_identity",
            ),
        )
        for column in (
            "knowledge_relation_id",
            "from_concept_revision_id",
            "to_concept_revision_id",
            "relation_type",
            "verification_status",
            "content_hash",
        ):
            _index("knowledge_relation_revisions", column)

    if "knowledge_network_relation_bindings" not in tables:
        op.create_table(
            "knowledge_network_relation_bindings",
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("knowledge_network_revision_id", sa.String(), nullable=False),
            sa.Column("knowledge_relation_revision_id", sa.String(), nullable=False),
            sa.Column("position", sa.Integer(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(
                ["knowledge_network_revision_id"], ["knowledge_network_revisions.id"]
            ),
            sa.ForeignKeyConstraint(
                ["knowledge_relation_revision_id"], ["knowledge_relation_revisions.id"]
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "knowledge_network_revision_id",
                "knowledge_relation_revision_id",
                name="uq_knowledge_network_relation_identity",
            ),
        )
        _index("knowledge_network_relation_bindings", "knowledge_network_revision_id")
        _index("knowledge_network_relation_bindings", "knowledge_relation_revision_id")

    if "capability_subnets" not in tables:
        op.create_table(
            "capability_subnets",
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("capability_revision_id", sa.String(), nullable=False),
            sa.Column("knowledge_network_revision_id", sa.String(), nullable=False),
            sa.Column("boundary_json", sa.Text(), nullable=False),
            sa.Column("context_json", sa.Text(), nullable=False),
            sa.Column("content_hash", sa.String(length=64), nullable=False),
            sa.Column("status", sa.String(length=24), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["capability_revision_id"], ["capability_revisions.id"]),
            sa.ForeignKeyConstraint(
                ["knowledge_network_revision_id"], ["knowledge_network_revisions.id"]
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("capability_revision_id", name="uq_capability_subnet_revision"),
            sa.UniqueConstraint("content_hash"),
        )
        for column in (
            "capability_revision_id",
            "knowledge_network_revision_id",
            "content_hash",
            "status",
        ):
            _index("capability_subnets", column)

    if "capability_relation_requirements" not in tables:
        op.create_table(
            "capability_relation_requirements",
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("capability_revision_id", sa.String(), nullable=False),
            sa.Column("knowledge_relation_revision_id", sa.String(), nullable=False),
            sa.Column("role", sa.String(length=24), nullable=False),
            sa.Column("required", sa.Boolean(), nullable=False),
            sa.Column("minimum_stage", sa.String(length=24), nullable=False),
            sa.Column("purpose", sa.String(length=40), nullable=False),
            sa.Column("position", sa.Integer(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["capability_revision_id"], ["capability_revisions.id"]),
            sa.ForeignKeyConstraint(
                ["knowledge_relation_revision_id"], ["knowledge_relation_revisions.id"]
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "capability_revision_id",
                "knowledge_relation_revision_id",
                name="uq_capability_relation_requirement_identity",
            ),
        )
        for column in (
            "capability_revision_id",
            "knowledge_relation_revision_id",
            "role",
            "minimum_stage",
        ):
            _index("capability_relation_requirements", column)

    if "assessment_target_concept_bindings" not in tables:
        op.create_table(
            "assessment_target_concept_bindings",
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("assessment_target_id", sa.String(), nullable=False),
            sa.Column("concept_revision_id", sa.String(), nullable=False),
            sa.Column("role", sa.String(length=24), nullable=False),
            sa.Column("required", sa.Boolean(), nullable=False),
            sa.Column("position", sa.Integer(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["assessment_target_id"], ["assessment_targets.id"]),
            sa.ForeignKeyConstraint(["concept_revision_id"], ["concept_revisions.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "assessment_target_id",
                "concept_revision_id",
                name="uq_assessment_target_concept_identity",
            ),
        )
        for column in ("assessment_target_id", "concept_revision_id", "role"):
            _index("assessment_target_concept_bindings", column)

    if "assessment_target_relation_bindings" not in tables:
        op.create_table(
            "assessment_target_relation_bindings",
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("assessment_target_id", sa.String(), nullable=False),
            sa.Column("knowledge_relation_revision_id", sa.String(), nullable=False),
            sa.Column("required", sa.Boolean(), nullable=False),
            sa.Column("position", sa.Integer(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["assessment_target_id"], ["assessment_targets.id"]),
            sa.ForeignKeyConstraint(
                ["knowledge_relation_revision_id"], ["knowledge_relation_revisions.id"]
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "assessment_target_id",
                "knowledge_relation_revision_id",
                name="uq_assessment_target_relation_identity",
            ),
        )
        for column in ("assessment_target_id", "knowledge_relation_revision_id"):
            _index("assessment_target_relation_bindings", column)


def downgrade():
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    for table in (
        "assessment_target_relation_bindings",
        "assessment_target_concept_bindings",
        "capability_relation_requirements",
        "capability_subnets",
        "knowledge_network_relation_bindings",
        "knowledge_relation_revisions",
        "knowledge_relations",
        "knowledge_network_concept_bindings",
        "knowledge_network_revisions",
        "knowledge_networks",
    ):
        if table in tables:
            op.drop_table(table)
