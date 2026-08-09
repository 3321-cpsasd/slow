"""Add versioned curriculum baseline authority.

Revision ID: 0042_curriculum_baselines
Revises: 0041_product_events
"""

from alembic import op
import sqlalchemy as sa


revision = "0042_curriculum_baselines"
down_revision = "0041_product_events"
branch_labels = None
depends_on = None


def upgrade():
    required_tables = {
        "curriculum_source_versions",
        "disciplines",
        "program_versions",
        "course_versions",
        "competencies",
        "curriculum_baseline_versions",
        "series_curriculum_baseline_bindings",
        "chapter_curriculum_objective_bindings",
    }
    existing_tables = set(sa.inspect(op.get_bind()).get_table_names())
    if required_tables.issubset(existing_tables):
        return
    partial = required_tables & existing_tables
    if partial:
        raise RuntimeError(
            "partial curriculum baseline schema requires explicit repair: "
            + ", ".join(sorted(partial))
        )
    op.create_table(
        "curriculum_source_versions",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("source_key", sa.String(160), nullable=False),
        sa.Column("source_type", sa.String(40), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("authority", sa.String(240), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("version_label", sa.String(160), nullable=False),
        sa.Column("publication_date", sa.String(32), nullable=False),
        sa.Column("applicability_json", sa.Text(), nullable=False),
        sa.Column("retrieval_date", sa.String(32), nullable=False),
        sa.Column("content_digest", sa.String(64), nullable=False),
        sa.Column("verification_status", sa.String(32), nullable=False),
        sa.Column("provenance_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "source_key",
            "version_label",
            name="uq_curriculum_source_key_version",
        ),
    )
    op.create_index(
        op.f("ix_curriculum_source_versions_source_key"),
        "curriculum_source_versions",
        ["source_key"],
    )
    op.create_index(
        op.f("ix_curriculum_source_versions_source_type"),
        "curriculum_source_versions",
        ["source_type"],
    )
    op.create_index(
        op.f("ix_curriculum_source_versions_verification_status"),
        "curriculum_source_versions",
        ["verification_status"],
    )

    op.create_table(
        "disciplines",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("code", sa.String(80), nullable=False),
        sa.Column("name", sa.String(240), nullable=False),
        sa.Column("jurisdiction", sa.String(80), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code"),
    )
    op.create_index(op.f("ix_disciplines_code"), "disciplines", ["code"])
    op.create_index(op.f("ix_disciplines_status"), "disciplines", ["status"])

    op.create_table(
        "program_versions",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("discipline_id", sa.String(), nullable=False),
        sa.Column("source_version_id", sa.String(), nullable=False),
        sa.Column("institution", sa.String(240), nullable=False),
        sa.Column("program_code", sa.String(80), nullable=False),
        sa.Column("name", sa.String(300), nullable=False),
        sa.Column("version_label", sa.String(160), nullable=False),
        sa.Column("applicability_json", sa.Text(), nullable=False),
        sa.Column("review_status", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["discipline_id"], ["disciplines.id"]),
        sa.ForeignKeyConstraint(
            ["source_version_id"], ["curriculum_source_versions.id"]
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "institution",
            "program_code",
            "version_label",
            name="uq_program_institution_code_version",
        ),
    )
    op.create_index(
        op.f("ix_program_versions_discipline_id"),
        "program_versions",
        ["discipline_id"],
    )
    op.create_index(
        op.f("ix_program_versions_source_version_id"),
        "program_versions",
        ["source_version_id"],
    )
    op.create_index(
        op.f("ix_program_versions_review_status"),
        "program_versions",
        ["review_status"],
    )

    op.create_table(
        "course_versions",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("program_version_id", sa.String(), nullable=False),
        sa.Column("course_code", sa.String(80), nullable=False),
        sa.Column("title", sa.String(300), nullable=False),
        sa.Column("version_label", sa.String(160), nullable=False),
        sa.Column("course_type", sa.String(80), nullable=False),
        sa.Column("credits_json", sa.Text(), nullable=False),
        sa.Column("assessment_json", sa.Text(), nullable=False),
        sa.Column("aliases_json", sa.Text(), nullable=False),
        sa.Column("review_status", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["program_version_id"], ["program_versions.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "program_version_id",
            "course_code",
            "version_label",
            name="uq_course_program_code_version",
        ),
    )
    op.create_index(
        op.f("ix_course_versions_program_version_id"),
        "course_versions",
        ["program_version_id"],
    )
    op.create_index(
        op.f("ix_course_versions_review_status"),
        "course_versions",
        ["review_status"],
    )

    op.create_table(
        "competencies",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("namespace", sa.String(160), nullable=False),
        sa.Column("competency_key", sa.String(160), nullable=False),
        sa.Column("statement", sa.Text(), nullable=False),
        sa.Column("competency_type", sa.String(40), nullable=False),
        sa.Column("verification_modes_json", sa.Text(), nullable=False),
        sa.Column("review_status", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "namespace",
            "competency_key",
            name="uq_competency_namespace_key",
        ),
    )
    op.create_index(
        op.f("ix_competencies_namespace"), "competencies", ["namespace"]
    )
    op.create_index(
        op.f("ix_competencies_review_status"),
        "competencies",
        ["review_status"],
    )

    op.create_table(
        "curriculum_baseline_versions",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("baseline_key", sa.String(160), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(300), nullable=False),
        sa.Column("discipline_id", sa.String(), nullable=False),
        sa.Column("program_version_id", sa.String(), nullable=False),
        sa.Column("course_version_id", sa.String(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("scope_json", sa.Text(), nullable=False),
        sa.Column("graph_json", sa.Text(), nullable=False),
        sa.Column("gaps_json", sa.Text(), nullable=False),
        sa.Column("source_version_ids_json", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("review_json", sa.Text(), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["course_version_id"], ["course_versions.id"]),
        sa.ForeignKeyConstraint(["discipline_id"], ["disciplines.id"]),
        sa.ForeignKeyConstraint(["program_version_id"], ["program_versions.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("content_hash"),
        sa.UniqueConstraint(
            "baseline_key",
            "version",
            name="uq_curriculum_baseline_key_version",
        ),
    )
    op.create_index(
        op.f("ix_curriculum_baseline_versions_baseline_key"),
        "curriculum_baseline_versions",
        ["baseline_key"],
    )
    for column in (
        "discipline_id",
        "program_version_id",
        "course_version_id",
        "status",
    ):
        op.create_index(
            op.f(f"ix_curriculum_baseline_versions_{column}"),
            "curriculum_baseline_versions",
            [column],
        )

    op.create_table(
        "series_curriculum_baseline_bindings",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("series_id", sa.String(), nullable=False),
        sa.Column("baseline_version_id", sa.String(), nullable=False),
        sa.Column("selection_reason", sa.Text(), nullable=False),
        sa.Column("selection_snapshot_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["baseline_version_id"], ["curriculum_baseline_versions.id"]
        ),
        sa.ForeignKeyConstraint(["series_id"], ["series.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("series_id"),
    )
    op.create_index(
        op.f("ix_series_curriculum_baseline_bindings_series_id"),
        "series_curriculum_baseline_bindings",
        ["series_id"],
        unique=True,
    )
    op.create_index(
        op.f("ix_series_curriculum_baseline_bindings_baseline_version_id"),
        "series_curriculum_baseline_bindings",
        ["baseline_version_id"],
    )

    op.create_table(
        "chapter_curriculum_objective_bindings",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("chapter_id", sa.String(), nullable=False),
        sa.Column("baseline_version_id", sa.String(), nullable=False),
        sa.Column("objective_key", sa.String(160), nullable=False),
        sa.Column("coverage_role", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["baseline_version_id"], ["curriculum_baseline_versions.id"]
        ),
        sa.ForeignKeyConstraint(["chapter_id"], ["chapters.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "chapter_id",
            "objective_key",
            name="uq_chapter_curriculum_objective",
        ),
    )
    for column in ("chapter_id", "baseline_version_id", "objective_key"):
        op.create_index(
            op.f(f"ix_chapter_curriculum_objective_bindings_{column}"),
            "chapter_curriculum_objective_bindings",
            [column],
        )


def downgrade():
    op.drop_table("chapter_curriculum_objective_bindings")
    op.drop_table("series_curriculum_baseline_bindings")
    op.drop_table("curriculum_baseline_versions")
    op.drop_table("competencies")
    op.drop_table("course_versions")
    op.drop_table("program_versions")
    op.drop_table("disciplines")
    op.drop_table("curriculum_source_versions")
