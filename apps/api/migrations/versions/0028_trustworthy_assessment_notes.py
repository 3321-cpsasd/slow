"""Add trustworthy assessment facts, projections, and layered notes.

Revision ID: 0028_trustworthy_assessment_notes
Revises: 0027_milestone_paths
"""

from alembic import op
import sqlalchemy as sa


revision = "0028_trustworthy_assessment_notes"
down_revision = "0027_milestone_paths"
branch_labels = None
depends_on = None


def _tables():
    return set(sa.inspect(op.get_bind()).get_table_names())


def _columns(table_name):
    return {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns(table_name)
    }


def upgrade():
    if "assessment_targets" not in _tables():
        op.create_table(
            "assessment_targets",
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("objective_key", sa.String(300), nullable=False),
            sa.Column("objective_statement", sa.Text(), nullable=False),
            sa.Column("dimension", sa.String(32), nullable=False, server_default="recognition"),
            sa.Column("target_depth", sa.String(32), nullable=False, server_default="standard"),
            sa.Column("status", sa.String(24), nullable=False, server_default="active"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "objective_key", "dimension", "target_depth",
                name="uq_assessment_targets_semantics",
            ),
        )
        op.create_index("ix_assessment_targets_status", "assessment_targets", ["status"])
    elif "section_id" in _columns("assessment_targets"):
        # An intermediate ORM schema scoped targets directly to sections.
        # Rebuild that provisional shape into the global target identity used
        # by the authoritative section_assessment_targets binding table.
        with op.batch_alter_table(
            "assessment_targets",
            recreate="always",
        ) as batch_op:
            batch_op.drop_index("ix_assessment_targets_section_id")
            batch_op.drop_column("section_id")
            batch_op.create_unique_constraint(
                "uq_assessment_targets_semantics",
                ["objective_key", "dimension", "target_depth"],
            )

    if "section_assessment_targets" not in _tables():
        op.create_table(
            "section_assessment_targets",
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("section_id", sa.String(), nullable=False),
            sa.Column("assessment_target_id", sa.String(), nullable=False),
            sa.Column("position", sa.Integer(), nullable=False),
            sa.Column("required", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("verification_policy", sa.String(40), nullable=False, server_default="choice_quiz_v1"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["section_id"], ["sections.id"]),
            sa.ForeignKeyConstraint(["assessment_target_id"], ["assessment_targets.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("section_id", "position", name="uq_section_assessment_targets_position"),
            sa.UniqueConstraint("section_id", "assessment_target_id", name="uq_section_assessment_targets_target"),
        )
        op.create_index("ix_section_assessment_targets_section_id", "section_assessment_targets", ["section_id"])
        op.create_index("ix_section_assessment_targets_assessment_target_id", "section_assessment_targets", ["assessment_target_id"])

    if "scoring_results" not in _tables():
        op.create_table(
            "scoring_results",
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("attempt_id", sa.String(), nullable=False),
            sa.Column("scoring_rule_version", sa.String(40), nullable=False, server_default="choice_exact_v2"),
            sa.Column("score", sa.Integer(), nullable=False),
            sa.Column("total", sa.Integer(), nullable=False),
            sa.Column("passed", sa.Boolean(), nullable=False),
            sa.Column("results_json", sa.Text(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["attempt_id"], ["quiz_attempts.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("attempt_id"),
        )
        op.create_index("ix_scoring_results_attempt_id", "scoring_results", ["attempt_id"], unique=True)

    if "assessment_observations" not in _tables():
        op.create_table(
            "assessment_observations",
            sa.Column("sequence", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("learning_run_id", sa.String(), nullable=False),
            sa.Column("user_id", sa.String(), nullable=False),
            sa.Column("section_id", sa.String(), nullable=False),
            sa.Column("attempt_id", sa.String(), nullable=False),
            sa.Column("scoring_result_id", sa.String(), nullable=False),
            sa.Column("assessment_target_id", sa.String(), nullable=False),
            sa.Column("question_index", sa.Integer(), nullable=False),
            sa.Column("correct", sa.Boolean(), nullable=False),
            sa.Column("assistance_mode", sa.String(32), nullable=False, server_default="unassisted_initial"),
            sa.Column("learning_episode_id", sa.String(120), nullable=False),
            sa.Column("equivalence_group_id", sa.String(120), nullable=False, server_default=""),
            sa.Column("qualification_at_creation", sa.String(24), nullable=False, server_default="eligible"),
            sa.Column("qualification_rule_version", sa.String(40), nullable=False, server_default="evidence_v1"),
            sa.Column("payload_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["learning_run_id", "user_id"], ["learning_runs.id", "learning_runs.user_id"], name="fk_assessment_observations_run_user"),
            sa.ForeignKeyConstraint(["section_id"], ["sections.id"]),
            sa.ForeignKeyConstraint(["attempt_id"], ["quiz_attempts.id"]),
            sa.ForeignKeyConstraint(["scoring_result_id"], ["scoring_results.id"]),
            sa.ForeignKeyConstraint(["assessment_target_id"], ["assessment_targets.id"]),
            sa.PrimaryKeyConstraint("sequence"),
            sa.UniqueConstraint("id"),
            sa.UniqueConstraint("attempt_id", "question_index", name="uq_assessment_observations_attempt_question"),
        )
        for column in ("id", "learning_run_id", "user_id", "section_id", "attempt_id", "scoring_result_id", "assessment_target_id", "learning_episode_id", "equivalence_group_id"):
            op.create_index(f"ix_assessment_observations_{column}", "assessment_observations", [column], unique=(column == "id"))

    if "evidence_qualification_events" not in _tables():
        op.create_table(
            "evidence_qualification_events",
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("observation_id", sa.String(), nullable=False),
            sa.Column("projection_family", sa.String(32), nullable=False),
            sa.Column("status", sa.String(24), nullable=False),
            sa.Column("reason", sa.Text(), nullable=False, server_default=""),
            sa.Column("rule_version", sa.String(40), nullable=False, server_default="evidence_v1"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["observation_id"], ["assessment_observations.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "observation_id", "projection_family", "rule_version",
                name="uq_evidence_qualification_observation_family_rule",
            ),
        )
        op.create_index("ix_evidence_qualification_events_observation_id", "evidence_qualification_events", ["observation_id"])
        op.create_index("ix_evidence_qualification_events_projection_family", "evidence_qualification_events", ["projection_family"])

    if "assessment_gate_states" not in _tables():
        op.create_table(
            "assessment_gate_states",
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("learning_run_id", sa.String(), nullable=False),
            sa.Column("user_id", sa.String(), nullable=False),
            sa.Column("section_id", sa.String(), nullable=False),
            sa.Column("assessment_target_id", sa.String(), nullable=False),
            sa.Column("status", sa.String(32), nullable=False, server_default="unresolved"),
            sa.Column("resolved_by_observation_id", sa.String(), nullable=True),
            sa.Column("projection_rule_version", sa.String(40), nullable=False, server_default="gate_v1"),
            sa.Column("projection_version", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("source_observation_watermark", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["learning_run_id", "user_id"], ["learning_runs.id", "learning_runs.user_id"], name="fk_assessment_gate_states_run_user"),
            sa.ForeignKeyConstraint(["section_id"], ["sections.id"]),
            sa.ForeignKeyConstraint(["assessment_target_id"], ["assessment_targets.id"]),
            sa.ForeignKeyConstraint(["resolved_by_observation_id"], ["assessment_observations.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("learning_run_id", "section_id", "assessment_target_id", name="uq_assessment_gate_states_run_section_target"),
        )
        for column in ("learning_run_id", "user_id", "section_id", "assessment_target_id", "status"):
            op.create_index(f"ix_assessment_gate_states_{column}", "assessment_gate_states", [column])

    if "knowledge_state_projections" not in _tables():
        op.create_table(
            "knowledge_state_projections",
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("user_id", sa.String(), nullable=False),
            sa.Column("assessment_target_id", sa.String(), nullable=False),
            sa.Column("p_known_ppm", sa.Integer(), nullable=False, server_default="200000"),
            sa.Column("uncertainty_ppm", sa.Integer(), nullable=False, server_default="1000000"),
            sa.Column("claim_status", sa.String(32), nullable=False, server_default="unobserved"),
            sa.Column("retention_rounds", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("parameter_set_version", sa.String(40), nullable=False, server_default="bkt_v1"),
            sa.Column("projection_rule_version", sa.String(40), nullable=False, server_default="mastery_v1"),
            sa.Column("projection_version", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("source_observation_watermark", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("rebuilt_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
            sa.ForeignKeyConstraint(["assessment_target_id"], ["assessment_targets.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("user_id", "assessment_target_id", name="uq_knowledge_state_user_target"),
        )
        op.create_index("ix_knowledge_state_projections_user_id", "knowledge_state_projections", ["user_id"])
        op.create_index("ix_knowledge_state_projections_assessment_target_id", "knowledge_state_projections", ["assessment_target_id"])
        op.create_index("ix_knowledge_state_projections_claim_status", "knowledge_state_projections", ["claim_status"])

    if "review_states" not in _tables():
        op.create_table(
            "review_states",
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("user_id", sa.String(), nullable=False),
            sa.Column("assessment_target_id", sa.String(), nullable=False),
            sa.Column("status", sa.String(24), nullable=False, server_default="scheduled"),
            sa.Column("next_due_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("priority", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("reason", sa.String(80), nullable=False, server_default="initial_learning"),
            sa.Column("spacing_stage", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("projection_rule_version", sa.String(40), nullable=False, server_default="review_v1"),
            sa.Column("projection_version", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("source_observation_watermark", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
            sa.ForeignKeyConstraint(["assessment_target_id"], ["assessment_targets.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("user_id", "assessment_target_id", name="uq_review_states_user_target"),
        )
        for column in ("user_id", "assessment_target_id", "status", "next_due_at"):
            op.create_index(f"ix_review_states_{column}", "review_states", [column])

    if "learning_note_summaries" not in _tables():
        op.create_table(
            "learning_note_summaries",
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("note_id", sa.String(), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False),
            sa.Column("content_json", sa.Text(), nullable=False),
            sa.Column("source_content_version_id", sa.String(), nullable=True),
            sa.Column("source_contract_version", sa.String(40), nullable=False, server_default="generated_note_v1"),
            sa.Column("source_observation_watermark", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("generation_rule_version", sa.String(40), nullable=False, server_default="note_summary_v1"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["note_id"], ["learning_notes.id"]),
            sa.ForeignKeyConstraint(["source_content_version_id"], ["content_versions.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("note_id", "version", name="uq_learning_note_summaries_note_version"),
        )
        op.create_index("ix_learning_note_summaries_note_id", "learning_note_summaries", ["note_id"])
    elif "source_contract_version" not in _columns("learning_note_summaries"):
        # Some development databases were created from an intermediate ORM
        # schema before this migration was stamped. Repair that known shape so
        # the legacy-note backfill below remains repeatable and non-destructive.
        op.add_column(
            "learning_note_summaries",
            sa.Column(
                "source_contract_version",
                sa.String(40),
                nullable=False,
                server_default="generated_note_v1",
            ),
        )

    if "learning_note_review_supplements" not in _tables():
        op.create_table(
            "learning_note_review_supplements",
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("note_id", sa.String(), nullable=False),
            sa.Column("review_episode_id", sa.String(120), nullable=False),
            sa.Column("content_json", sa.Text(), nullable=False),
            sa.Column("author_kind", sa.String(24), nullable=False, server_default="user"),
            sa.Column("source_observation_watermark", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["note_id"], ["learning_notes.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("note_id", "review_episode_id", name="uq_learning_note_review_supplements_episode"),
        )
        op.create_index("ix_learning_note_review_supplements_note_id", "learning_note_review_supplements", ["note_id"])
        op.create_index("ix_learning_note_review_supplements_review_episode_id", "learning_note_review_supplements", ["review_episode_id"])

    if "learning_note_user_revisions" not in _tables():
        op.create_table(
            "learning_note_user_revisions",
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("note_id", sa.String(), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False),
            sa.Column("content_json", sa.Text(), nullable=False),
            sa.Column("based_on_summary_version", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("source", sa.String(24), nullable=False, server_default="user_edit"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["note_id"], ["learning_notes.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("note_id", "version", name="uq_learning_note_user_revisions_note_version"),
        )
        op.create_index("ix_learning_note_user_revisions_note_id", "learning_note_user_revisions", ["note_id"])

    # Preserve existing note content as explicit layer-1 and layer-3 versions.
    bind = op.get_bind()
    if "learning_note_summaries" in _tables():
        bind.execute(sa.text("""
            INSERT INTO learning_note_summaries
                (id, note_id, version, content_json, source_content_version_id,
                 source_contract_version, source_observation_watermark,
                 generation_rule_version, created_at)
            SELECT 'note_summary_migrated_' || id, id, 1, ai_content_json, NULL,
                   'legacy_learning_note_v1',
                   0, 'legacy_note_import_v1', updated_at
            FROM learning_notes
            WHERE NOT EXISTS (
                SELECT 1 FROM learning_note_summaries s WHERE s.note_id = learning_notes.id
            )
        """))
        bind.execute(sa.text("""
            INSERT INTO learning_note_user_revisions
                (id, note_id, version, content_json, based_on_summary_version,
                 source, created_at)
            SELECT 'note_user_revision_migrated_' || id, id, 1,
                   user_content_json, 1, 'legacy_user_import_v1', updated_at
            FROM learning_notes
            WHERE user_content_json IS NOT NULL
              AND user_content_json != '{}'
              AND NOT EXISTS (
                SELECT 1 FROM learning_note_user_revisions r WHERE r.note_id = learning_notes.id
            )
        """))


def downgrade():
    for table in (
        "learning_note_user_revisions",
        "learning_note_review_supplements",
        "learning_note_summaries",
        "review_states",
        "knowledge_state_projections",
        "assessment_gate_states",
        "evidence_qualification_events",
        "assessment_observations",
        "scoring_results",
        "section_assessment_targets",
        "assessment_targets",
    ):
        if table in _tables():
            op.drop_table(table)
