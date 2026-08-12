"""Add M3 continuity, diagnosis, and preference-authority foundations.

Revision ID: 0054_m3_pilot_foundations
Revises: 0053_lesson_case_identity
"""

from alembic import op
import sqlalchemy as sa


revision = "0054_m3_pilot_foundations"
down_revision = "0053_lesson_case_identity"
branch_labels = None
depends_on = None


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _columns(table_name: str) -> set[str]:
    return {
        item["name"]
        for item in sa.inspect(op.get_bind()).get_columns(table_name)
    }


def upgrade():
    tables = _tables()
    if "learning_preference_decisions" not in tables:
        op.create_table(
            "learning_preference_decisions",
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("user_id", sa.String(), nullable=False),
            sa.Column("decision_key", sa.String(length=128), nullable=False),
            sa.Column("decision_sequence", sa.Integer(), nullable=False),
            sa.Column("scope_kind", sa.String(length=16), nullable=False),
            sa.Column("shelf_id", sa.String(), nullable=True),
            sa.Column("dimension", sa.String(length=32), nullable=False),
            sa.Column("state", sa.String(length=16), nullable=False),
            sa.Column("source_evidence_id", sa.String(), nullable=True),
            sa.Column("request_hash", sa.String(length=64), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
            sa.ForeignKeyConstraint(["shelf_id"], ["shelves.id"]),
            sa.ForeignKeyConstraint(
                ["source_evidence_id"], ["learning_preference_evidence.id"]
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "user_id",
                "decision_key",
                name="uq_learning_preference_decisions_user_key",
            ),
            sa.UniqueConstraint(
                "user_id",
                "decision_sequence",
                name="uq_learning_preference_decisions_user_sequence",
            ),
        )
        op.create_index(
            "ix_learning_preference_decisions_user_id",
            "learning_preference_decisions",
            ["user_id"],
        )
        op.create_index(
            "ix_learning_preference_decisions_shelf_id",
            "learning_preference_decisions",
            ["shelf_id"],
        )
        op.create_index(
            "ix_learning_preference_decisions_dimension",
            "learning_preference_decisions",
            ["dimension"],
        )
        op.create_index(
            "ix_learning_preference_decisions_state",
            "learning_preference_decisions",
            ["state"],
        )
        op.create_index(
            "ix_learning_preference_decisions_source_evidence_id",
            "learning_preference_decisions",
            ["source_evidence_id"],
        )
        op.create_index(
            "ix_learning_preference_decisions_scope",
            "learning_preference_decisions",
            ["user_id", "scope_kind", "shelf_id", "dimension", "created_at"],
        )

    series_columns = _columns("series")
    if "continuity_tier" not in series_columns:
        op.add_column(
            "series",
            sa.Column(
                "continuity_tier",
                sa.String(length=24),
                nullable=False,
                server_default="recoverable",
            ),
        )
        op.create_index(
            "ix_series_continuity_tier", "series", ["continuity_tier"]
        )

    tables = _tables()
    if "standard_lesson_package_versions" not in tables:
        op.create_table(
            "standard_lesson_package_versions",
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("package_key", sa.String(length=160), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False),
            sa.Column("title", sa.String(length=300), nullable=False),
            sa.Column("contract_signature", sa.String(length=64), nullable=False),
            sa.Column("contract_snapshot_json", sa.Text(), nullable=False),
            sa.Column(
                "composition_policy_json", sa.Text(), nullable=False, server_default="{}"
            ),
            sa.Column("blocks_json", sa.Text(), nullable=False),
            sa.Column("questions_json", sa.Text(), nullable=False),
            sa.Column("sources_json", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("status", sa.String(length=24), nullable=False),
            sa.Column("review_status", sa.String(length=24), nullable=False),
            sa.Column("rights_status", sa.String(length=32), nullable=False),
            sa.Column("factual_status", sa.String(length=32), nullable=False),
            sa.Column("schema_version", sa.String(length=48), nullable=False),
            sa.Column("rule_version", sa.String(length=48), nullable=False),
            sa.Column("output_hash", sa.String(length=64), nullable=False),
            sa.Column("review_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "package_key",
                "version",
                name="uq_standard_lesson_package_key_version",
            ),
            sa.UniqueConstraint("output_hash"),
        )
        for name, columns in (
            ("ix_standard_lesson_package_versions_package_key", ["package_key"]),
            ("ix_standard_lesson_package_versions_contract_signature", ["contract_signature"]),
            ("ix_standard_lesson_package_versions_status", ["status"]),
            ("ix_standard_lesson_package_versions_review_status", ["review_status"]),
        ):
            op.create_index(name, "standard_lesson_package_versions", columns)

    tables = _tables()
    if "standard_lesson_package_targets" not in tables:
        op.create_table(
            "standard_lesson_package_targets",
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("package_version_id", sa.String(), nullable=False),
            sa.Column("assessment_target_id", sa.String(), nullable=False),
            sa.Column("position", sa.Integer(), nullable=False),
            sa.Column("required", sa.Boolean(), nullable=False),
            sa.Column("verification_policy", sa.String(length=48), nullable=False),
            sa.Column("target_depth", sa.String(length=32), nullable=False),
            sa.ForeignKeyConstraint(
                ["package_version_id"], ["standard_lesson_package_versions.id"]
            ),
            sa.ForeignKeyConstraint(
                ["assessment_target_id"], ["assessment_targets.id"]
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "package_version_id",
                "position",
                name="uq_standard_lesson_package_target_position",
            ),
            sa.UniqueConstraint(
                "package_version_id",
                "assessment_target_id",
                name="uq_standard_lesson_package_target_identity",
            ),
        )
        op.create_index(
            "ix_standard_lesson_package_targets_package_version_id",
            "standard_lesson_package_targets",
            ["package_version_id"],
        )
        op.create_index(
            "ix_standard_lesson_package_targets_assessment_target_id",
            "standard_lesson_package_targets",
            ["assessment_target_id"],
        )

    tables = _tables()
    if "section_fallback_bindings" not in tables:
        op.create_table(
            "section_fallback_bindings",
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("section_id", sa.String(), nullable=False),
            sa.Column("learning_contract_version_id", sa.String(), nullable=False),
            sa.Column("standard_package_version_id", sa.String(), nullable=False),
            sa.Column("contract_signature", sa.String(length=64), nullable=False),
            sa.Column("status", sa.String(length=24), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["section_id"], ["sections.id"]),
            sa.ForeignKeyConstraint(
                ["learning_contract_version_id"], ["learning_contract_versions.id"]
            ),
            sa.ForeignKeyConstraint(
                ["standard_package_version_id"], ["standard_lesson_package_versions.id"]
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "learning_contract_version_id",
                name="uq_section_fallback_binding_contract",
            ),
        )
        for name, columns in (
            ("ix_section_fallback_bindings_section_id", ["section_id"]),
            ("ix_section_fallback_bindings_learning_contract_version_id", ["learning_contract_version_id"]),
            ("ix_section_fallback_bindings_standard_package_version_id", ["standard_package_version_id"]),
            ("ix_section_fallback_bindings_contract_signature", ["contract_signature"]),
            ("ix_section_fallback_bindings_status", ["status"]),
        ):
            op.create_index(name, "section_fallback_bindings", columns)

    tables = _tables()
    if "route_admission_decisions" not in tables:
        op.create_table(
            "route_admission_decisions",
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("series_id", sa.String(), nullable=False),
            sa.Column("allowed", sa.Boolean(), nullable=False),
            sa.Column("covered_contracts", sa.Integer(), nullable=False),
            sa.Column("required_contracts", sa.Integer(), nullable=False),
            sa.Column("reasons_json", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("rule_version", sa.String(length=48), nullable=False),
            sa.Column("input_hash", sa.String(length=64), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["series_id"], ["series.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            "ix_route_admission_decisions_series_id",
            "route_admission_decisions",
            ["series_id"],
        )
        op.create_index(
            "ix_route_admission_decisions_allowed",
            "route_admission_decisions",
            ["allowed"],
        )
        op.create_index(
            "ix_route_admission_decisions_rule_version",
            "route_admission_decisions",
            ["rule_version"],
        )

    content_columns = _columns("content_versions")
    if "standard_package_version_id" not in content_columns:
        with op.batch_alter_table("content_versions") as batch_op:
            batch_op.add_column(
                sa.Column("standard_package_version_id", sa.String(), nullable=True)
            )
            batch_op.create_foreign_key(
                "fk_content_versions_standard_package",
                "standard_lesson_package_versions",
                ["standard_package_version_id"],
                ["id"],
            )
        op.create_index(
            "ix_content_versions_standard_package_version_id",
            "content_versions",
            ["standard_package_version_id"],
        )

    tables = _tables()
    if "assessment_distractor_diagnostics" not in tables:
        op.create_table(
            "assessment_distractor_diagnostics",
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("assessment_item_version_id", sa.String(), nullable=False),
            sa.Column("option_index", sa.Integer(), nullable=False),
            sa.Column("option_hash", sa.String(length=64), nullable=False),
            sa.Column("cause_code", sa.String(length=48), nullable=False),
            sa.Column("rationale", sa.Text(), nullable=False, server_default=""),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(
                ["assessment_item_version_id"], ["assessment_item_versions.id"]
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "assessment_item_version_id",
                "option_index",
                name="uq_assessment_distractor_item_option",
            ),
        )
        op.create_index(
            "ix_assessment_distractor_diagnostics_item",
            "assessment_distractor_diagnostics",
            ["assessment_item_version_id"],
        )
        op.create_index(
            "ix_assessment_distractor_diagnostics_cause",
            "assessment_distractor_diagnostics",
            ["cause_code"],
        )

    tables = _tables()
    if "remediation_diagnoses" not in tables:
        op.create_table(
            "remediation_diagnoses",
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("attempt_id", sa.String(), nullable=False),
            sa.Column("assessment_target_id", sa.String(), nullable=False),
            sa.Column("cause_code", sa.String(length=48), nullable=False),
            sa.Column("status", sa.String(length=24), nullable=False),
            sa.Column("confidence", sa.Float(), nullable=False),
            sa.Column("evidence_json", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("input_hash", sa.String(length=64), nullable=False),
            sa.Column("rule_version", sa.String(length=48), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["attempt_id"], ["quiz_attempts.id"]),
            sa.ForeignKeyConstraint(
                ["assessment_target_id"], ["assessment_targets.id"]
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "attempt_id",
                "assessment_target_id",
                "rule_version",
                name="uq_remediation_diagnosis_attempt_target_rule",
            ),
        )
        for name, columns in (
            ("ix_remediation_diagnoses_attempt_id", ["attempt_id"]),
            ("ix_remediation_diagnoses_assessment_target_id", ["assessment_target_id"]),
            ("ix_remediation_diagnoses_cause_code", ["cause_code"]),
            ("ix_remediation_diagnoses_status", ["status"]),
            ("ix_remediation_diagnoses_rule_version", ["rule_version"]),
        ):
            op.create_index(name, "remediation_diagnoses", columns)

    remediation_columns = _columns("remediations")
    if "diagnosis_snapshot_json" not in remediation_columns:
        op.add_column(
            "remediations",
            sa.Column(
                "diagnosis_snapshot_json",
                sa.Text(),
                nullable=False,
                server_default="[]",
            ),
        )


def downgrade():
    remediation_columns = _columns("remediations")
    if "diagnosis_snapshot_json" in remediation_columns:
        op.drop_column("remediations", "diagnosis_snapshot_json")
    for table_name in (
        "remediation_diagnoses",
        "assessment_distractor_diagnostics",
    ):
        if table_name in _tables():
            op.drop_table(table_name)
    content_columns = _columns("content_versions")
    if "standard_package_version_id" in content_columns:
        op.drop_index(
            "ix_content_versions_standard_package_version_id",
            table_name="content_versions",
        )
        with op.batch_alter_table("content_versions") as batch_op:
            batch_op.drop_constraint(
                "fk_content_versions_standard_package", type_="foreignkey"
            )
            batch_op.drop_column("standard_package_version_id")
    for table_name in (
        "route_admission_decisions",
        "section_fallback_bindings",
        "standard_lesson_package_targets",
        "standard_lesson_package_versions",
    ):
        if table_name in _tables():
            op.drop_table(table_name)
    if "continuity_tier" in _columns("series"):
        op.drop_index("ix_series_continuity_tier", table_name="series")
        op.drop_column("series", "continuity_tier")
    if "learning_preference_decisions" in _tables():
        op.drop_table("learning_preference_decisions")
