"""Add reviewed cross-series concept, relation, and capability identities.

Revision ID: 0070_cross_series_identity_publication
Revises: 0069_chapter_capability_planning
"""

from alembic import op
import sqlalchemy as sa


revision = "0070_cross_series_identity_publication"
down_revision = "0069_chapter_capability_planning"
branch_labels = None
depends_on = None


_ALIASES = {
    "published_concept_identities": "pub_concept",
    "published_relation_identities": "pub_relation",
    "published_capability_identities": "pub_capability",
    "identity_publication_decisions": "identity_pub_decision",
}


def _index(table: str, column: str) -> None:
    name = f"ix_{_ALIASES[table]}_{column}"
    if len(name) > 63:
        raise ValueError(f"PostgreSQL index identifier is too long: {name}")
    op.create_index(name, table, [column])


def _published_table(
    table: str,
    *,
    kind: str,
    revision_column: str,
    revision_table: str,
) -> None:
    op.create_table(
        table,
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("family_key", sa.String(length=240 if "relation" in table else 160), nullable=False),
        sa.Column("semantic_hash", sa.String(length=64), nullable=False),
        sa.Column(revision_column, sa.String(), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("supersedes_id", sa.String(), nullable=True),
        sa.Column("review_json", sa.Text(), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint([revision_column], [f"{revision_table}.id"]),
        sa.ForeignKeyConstraint(["supersedes_id"], [f"{table}.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "semantic_hash", name=f"uq_published_{kind}_identity_semantic_hash"
        ),
        sa.UniqueConstraint(
            revision_column, name=f"uq_published_{kind}_identity_revision"
        ),
    )
    for column in (
        "family_key",
        "semantic_hash",
        revision_column,
        "status",
        "supersedes_id",
    ):
        _index(table, column)


def upgrade():
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "published_concept_identities" not in tables:
        _published_table(
            "published_concept_identities",
            kind="concept",
            revision_column="concept_revision_id",
            revision_table="concept_revisions",
        )
    if "published_relation_identities" not in tables:
        _published_table(
            "published_relation_identities",
            kind="relation",
            revision_column="knowledge_relation_revision_id",
            revision_table="knowledge_relation_revisions",
        )
    if "published_capability_identities" not in tables:
        _published_table(
            "published_capability_identities",
            kind="capability",
            revision_column="capability_revision_id",
            revision_table="capability_revisions",
        )
    if "identity_publication_decisions" not in tables:
        op.create_table(
            "identity_publication_decisions",
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("subject_kind", sa.String(length=24), nullable=False),
            sa.Column("candidate_id", sa.String(length=200), nullable=False),
            sa.Column("decision", sa.String(length=32), nullable=False),
            sa.Column("resolved_revision_id", sa.String(length=200), nullable=True),
            sa.Column("compared_revision_ids_json", sa.Text(), nullable=False),
            sa.Column("basis_json", sa.Text(), nullable=False),
            sa.Column("actor_kind", sa.String(length=32), nullable=False),
            sa.Column("actor_id", sa.String(length=160), nullable=False),
            sa.Column("rule_version", sa.String(length=48), nullable=False),
            sa.Column("supersedes_id", sa.String(), nullable=True),
            sa.Column("decision_hash", sa.String(length=64), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(
                ["supersedes_id"], ["identity_publication_decisions.id"]
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "decision_hash", name="uq_identity_publication_decision_hash"
            ),
        )
        for column in (
            "subject_kind",
            "candidate_id",
            "decision",
            "resolved_revision_id",
            "rule_version",
            "supersedes_id",
            "created_at",
        ):
            _index("identity_publication_decisions", column)


def downgrade():
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    for table in (
        "identity_publication_decisions",
        "published_capability_identities",
        "published_relation_identities",
        "published_concept_identities",
    ):
        if table in tables:
            op.drop_table(table)
