import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest
from sqlalchemy import create_engine

from app.infrastructure.tables import Base


API_ROOT = Path(__file__).resolve().parents[1]
HEAD_REVISION = "0062_learning_start_choices"

pytestmark = pytest.mark.migration


def run_alembic(database: Path, *arguments: str) -> None:
    environment = {
        **os.environ,
        "DATABASE_URL": f"sqlite+pysqlite:///{database}",
        "PYTHONPATH": ".",
    }
    subprocess.run(
        [sys.executable, "-m", "alembic", *arguments],
        cwd=API_ROOT,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )


def recreate_sqlite_table_without(
    connection: sqlite3.Connection,
    table_name: str,
    fragments: tuple[str, ...],
) -> None:
    schema = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()[0]
    indexes = [
        row[0]
        for row in connection.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type = 'index' AND tbl_name = ? AND sql IS NOT NULL",
            (table_name,),
        )
    ]
    historical_name = f"{table_name}_historical"
    historical_schema = schema.replace(
        f"CREATE TABLE {table_name}",
        f"CREATE TABLE {historical_name}",
        1,
    )
    for fragment in fragments:
        assert fragment in historical_schema
        historical_schema = historical_schema.replace(fragment, "", 1)

    connection.execute("PRAGMA foreign_keys=OFF")
    connection.execute(historical_schema)
    columns = [
        row[1]
        for row in connection.execute(
            f'PRAGMA table_info("{historical_name}")'
        )
    ]
    column_list = ", ".join(f'"{column}"' for column in columns)
    connection.execute(
        f'INSERT INTO "{historical_name}" ({column_list}) '
        f'SELECT {column_list} FROM "{table_name}"'
    )
    connection.execute(f'DROP TABLE "{table_name}"')
    connection.execute(
        f'ALTER TABLE "{historical_name}" RENAME TO "{table_name}"'
    )
    for statement in indexes:
        connection.execute(statement)
    connection.commit()
    connection.execute("PRAGMA foreign_keys=ON")


def test_fresh_database_migrates_to_combined_head(tmp_path):
    database = tmp_path / "fresh.db"
    run_alembic(database, "upgrade", "head")

    with sqlite3.connect(database) as connection:
        revision = connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()[0]
        attempt_schema = connection.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type = 'table' AND name = 'quiz_attempts'"
        ).fetchone()[0]
        quiz_columns = {
            row[1]: row
            for row in connection.execute("PRAGMA table_info(quiz_sets)")
        }
        learning_task_schema = connection.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type = 'table' AND name = 'learning_tasks'"
        ).fetchone()[0]
        task_columns = {
            row[1]: row
            for row in connection.execute("PRAGMA table_info(learning_tasks)")
        }
        section_columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(sections)")
        }
        generation_lease_schema = connection.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type = 'table' AND name = 'generation_leases'"
        ).fetchone()[0]
        invocation_columns = {
            row[1]: row
            for row in connection.execute("PRAGMA table_info(ai_invocations)")
        }
        measurement_schema = connection.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type = 'table' AND name = 'ai_usage_measurements'"
        ).fetchone()[0]
        auth_session_schema = connection.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type = 'table' AND name = 'auth_sessions'"
        ).fetchone()[0]
        resume_schema = connection.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type = 'table' AND name = 'learning_resume_positions'"
        ).fetchone()[0]
        artifact_submission_schema = connection.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type = 'table' AND name = 'artifact_submissions'"
        ).fetchone()[0]
        local_credential_columns = {
            row[1]: row
            for row in connection.execute("PRAGMA table_info(local_credentials)")
        }
        recovery_code_schema = connection.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type = 'table' AND name = 'account_recovery_codes'"
        ).fetchone()[0]
        recovery_code_columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(account_recovery_codes)"
            )
        }
        content_block_columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(content_block_versions)"
            )
        }
        remediation_columns = {
            row[1]: row
            for row in connection.execute("PRAGMA table_info(remediations)")
        }
        remediation_indexes = list(
            connection.execute("PRAGMA index_list(remediations)")
        )
        profile_columns = {
            row[1]: row
            for row in connection.execute("PRAGMA table_info(user_profiles)")
        }
        onboarding_columns = {
            row[1]: row
            for row in connection.execute("PRAGMA table_info(user_onboardings)")
        }
        shelf_columns = {
            row[1]: row
            for row in connection.execute("PRAGMA table_info(shelves)")
        }
        milestone_path_columns = {
            row[1]: row
            for row in connection.execute("PRAGMA table_info(milestone_paths)")
        }
        milestone_revision_indexes = list(
            connection.execute("PRAGMA index_list(milestone_path_revisions)")
        )
        trustworthy_tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert {
            "user_daily_mode_states",
            "daily_mode_events",
            "learning_preference_evidence",
            "learning_preference_decisions",
            "personal_block_presentations",
            "alpha_registration_quotas",
            "standard_lesson_package_versions",
            "standard_lesson_package_targets",
            "section_fallback_bindings",
            "route_admission_decisions",
            "learning_evidence_invalidations",
            "assessment_distractor_diagnostics",
            "remediation_diagnoses",
            "learning_start_previews",
            "series_learning_start_preferences",
            "chapter_route_decision_events",
            "chapter_challenge_attempts",
        }.issubset(trustworthy_tables)
        qa_message_columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(qa_messages)")
        }
        preference_evidence_columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(learning_preference_evidence)"
            )
        }
        assessment_target_columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(assessment_targets)")
        }
        observation_columns = {
            row[1]: row
            for row in connection.execute(
                "PRAGMA table_info(assessment_observations)"
            )
        }
        discussion_turn_columns = {
            row[1]: row
            for row in connection.execute(
                "PRAGMA table_info(ask_me_discussion_turns)"
            )
        }
        discussion_turn_indexes = list(connection.execute(
            "PRAGMA index_list(ask_me_discussion_turns)"
        ))
        gate_columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(assessment_gate_states)")
        }
        knowledge_columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(knowledge_state_projections)")
        }
        knowledge_node_columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(knowledge_node_state_projections)"
            )
        }
        review_columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(review_states)")
        }
        series_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(series)")
        }
        book_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(books)")
        }
        run_columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(learning_runs)")
        }
        qa_session_columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(qa_sessions)")
        }
        assert "daily_mode" in qa_session_columns
        content_columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(content_versions)")
        }
        quiz_binding_columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(quiz_sets)")
        }
        feedback_columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(user_feedback)")
        }
        feedback_schema = connection.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type = 'table' AND name = 'user_feedback'"
        ).fetchone()[0]
        privacy_consent_schema = connection.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type = 'table' AND name = 'privacy_consents'"
        ).fetchone()[0]
        account_exit_schema = connection.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type = 'table' AND name = 'account_exit_requests'"
        ).fetchone()[0]
        product_event_schema = connection.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type = 'table' AND name = 'product_events'"
        ).fetchone()[0]
        study_activity_schema = connection.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type = 'table' AND name = 'study_activity_pulses'"
        ).fetchone()[0]
        curriculum_baseline_schema = connection.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type = 'table' AND name = 'curriculum_baseline_versions'"
        ).fetchone()[0]
        chapter_baseline_schema = connection.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type = 'table' AND name = 'chapter_curriculum_objective_bindings'"
        ).fetchone()[0]

    assert revision == HEAD_REVISION
    assert {"registration_source", "registration_quota_date"}.issubset(
        local_credential_columns
    )
    assert {
        "preference_request_event_id",
        "explanation_style",
        "explanation_block_kind",
        "request_source",
    }.issubset(qa_message_columns)
    assert "terminal_request_key" in preference_evidence_columns
    assert "continuity_tier" in series_columns
    assert "standard_package_version_id" in content_columns
    assert "diagnosis_snapshot_json" in remediation_columns
    assert {
        "teaching_moves_json",
        "case_kind",
        "case_key",
        "relation_to_anchor",
        "reader_priority",
    }.issubset(content_block_columns)
    assert "uq_privacy_consents_user_versions" in privacy_consent_schema
    assert "deletion_due_at" in account_exit_schema
    assert "uq_product_events_user_event" in product_event_schema
    assert "uq_study_activity_pulses_user_event" in study_activity_schema
    assert "uq_curriculum_baseline_key_version" in curriculum_baseline_schema
    assert "uq_chapter_curriculum_objective" in chapter_baseline_schema
    assert {
        "outline_status",
        "outline_version",
        "outline_confirmed_at",
    }.issubset(book_columns)
    assert "uq_quiz_attempts_run_user_idempotency" in attempt_schema
    assert "uq_quiz_attempts_user_id_idempotency_key" not in attempt_schema
    assert quiz_columns["content_version_id"][3] == 1
    assert "uq_learning_tasks_run_type_idempotency" in learning_task_schema
    assert task_columns["section_id"][3] == 0
    assert {
        "lease_owner",
        "lease_token",
        "lease_expires_at",
        "heartbeat_at",
    }.issubset(task_columns)
    assert {
        "status",
        "best_score",
        "total_score",
        "ask_me_unlocked",
    }.isdisjoint(section_columns)
    assert "resource_key" in generation_lease_schema
    assert "UNIQUE (owner_id)" in generation_lease_schema
    assert invocation_columns["subject_user_id"][3] == 0
    assert {
        "purpose",
        "authority",
        "deployment_id",
        "model_family_id",
        "config_version_id",
        "route_policy_version",
        "fallback_index",
    }.issubset(invocation_columns)
    assert "uq_ai_usage_measurement_source_version" in measurement_schema
    assert "token_hash" in auth_session_schema
    assert "fk_learning_resume_run_user" in resume_schema
    assert "fk_artifact_submissions_run_user" in artifact_submission_schema
    assert {
        "username",
        "password_hash",
        "failed_attempts",
        "locked_until",
    }.issubset(local_credential_columns)
    assert "uq_account_recovery_codes_user_version" in recovery_code_schema
    assert "code_hash" in recovery_code_schema
    assert "raw_code" not in recovery_code_columns
    assert "supersedes_id" in remediation_columns
    assert any(
        row[1] == "ix_remediations_attempt_id" and row[2] == 0
        for row in remediation_indexes
    )
    assert {
        "profession",
        "stage",
        "purpose",
        "domains_json",
        "completed_at",
    }.issubset(profile_columns)
    assert {"flow_id", "status", "current_step"}.issubset(
        onboarding_columns
    )
    assert shelf_columns["origin"][3] == 1
    assert {"weekly_minutes", "target_date"}.issubset(profile_columns)
    assert "preferences_json" in profile_columns
    assert profile_columns["preferences_json"][3] == 1
    assert {
        "user_id",
        "series_id",
        "goal_profile_version",
        "definition_json",
        "ruleset_version",
    }.issubset(milestone_path_columns)
    assert {
        "assessment_targets",
        "section_assessment_targets",
        "scoring_results",
        "assessment_observations",
        "evidence_qualification_events",
        "assessment_gate_states",
        "knowledge_state_projections",
        "knowledge_node_state_projections",
        "review_states",
        "learning_note_summaries",
        "learning_note_review_supplements",
        "learning_note_user_revisions",
        "learning_mission_versions",
        "mission_success_criteria",
        "mission_success_criterion_versions",
        "mission_adoption_events",
        "ask_me_discussion_sessions",
        "ask_me_discussion_topics",
        "ask_me_discussion_turns",
        "ask_me_discussion_commands",
        "concepts",
        "concept_revisions",
        "learning_objectives",
        "learning_contract_versions",
        "learning_contract_concepts",
        "learning_contract_objectives",
        "learning_contract_assessment_targets",
        "learning_run_section_bindings",
        "source_versions",
        "content_block_versions",
        "source_claims",
        "source_claim_versions",
        "content_block_claim_anchors",
        "source_claim_bindings",
        "knowledge_gaps",
        "knowledge_gap_events",
        "knowledge_graph_releases",
        "knowledge_source_versions",
        "concept_relation_versions",
        "concept_objective_bindings",
        "knowledge_claim_bindings",
        "governance_decision_snapshots",
        "review_selection_runs",
        "review_assignments",
        "review_assignment_events",
        "learning_decision_snapshots",
        "user_feedback",
    }.issubset(trustworthy_tables)
    assert "section_id" not in assessment_target_columns
    assert {"source_type", "evidence_key"}.issubset(observation_columns)
    assert observation_columns["attempt_id"][3] == 0
    assert observation_columns["scoring_result_id"][3] == 0
    assert observation_columns["question_index"][3] == 0
    assert {"lease_token", "lease_expires_at"}.issubset(
        discussion_turn_columns
    )
    assert not any(
        row[1] == "uq_ask_me_discussion_turns_topic_index"
        for row in discussion_turn_indexes
    )
    assert {
        "concept_revision_id",
        "learning_objective_id",
        "identity_status",
    }.issubset(assessment_target_columns)
    assert "projection_version" in gate_columns
    assert "projection_version" in knowledge_columns
    assert {
        "user_id",
        "concept_revision_id",
        "current_rank",
        "current_stars",
        "highest_rank",
        "activation_state",
        "next_due_at",
        "rank_rule_version",
        "source_observation_watermark",
    }.issubset(knowledge_node_columns)
    assert "projection_version" in review_columns
    assert "initial_mission_version_id" in series_columns
    assert "initial_mission_version_id" in run_columns
    assert "learning_contract_version_id" in content_columns
    assert {
        "generation_mode",
        "rights_status",
        "factual_status",
        "ai_generated",
        "generation_run_id",
        "output_hash",
        "labeling_metadata_json",
    }.issubset(content_columns)
    assert "learning_contract_version_id" in quiz_binding_columns
    assert {"idempotency_key", "request_hash"}.issubset(feedback_columns)
    assert "uq_user_feedback_user_idempotency" in feedback_schema
    assert any(
        row[1] == "sqlite_autoindex_milestone_path_revisions_2" or row[2] == 1
        for row in milestone_revision_indexes
    )


def test_0048_empty_database_downgrades_and_upgrades(tmp_path):
    database = tmp_path / "0048-empty-round-trip.db"
    run_alembic(database, "upgrade", "head")
    run_alembic(
        database,
        "downgrade",
        "0047_historical_schema_repair",
    )
    run_alembic(database, "upgrade", "head")


def test_0056_backfills_rank_qualification_without_rewriting_v2(tmp_path):
    database = tmp_path / "0056-rank-qualification.db"
    run_alembic(database, "upgrade", "0054_m3_pilot_foundations")
    timestamp = "2026-08-12 12:00:00"
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.executescript(
            f"""
            INSERT INTO users (id, name, status, created_at, updated_at)
            VALUES ('user_rank_migration', 'Rank migration', 'active', '{timestamp}', '{timestamp}');
            INSERT INTO shelves (
                id, user_id, name, domain, specialty, tags_json, origin
            ) VALUES (
                'shelf_rank_migration', 'user_rank_migration', 'Rank',
                'Test', 'Migration', '[]', 'demo_seed'
            );
            INSERT INTO learning_plans (
                id, shelf_id, topic, role, experience, purpose, depth,
                details, assumptions_json, confidence, status, created_at
            ) VALUES (
                'plan_rank_migration', 'shelf_rank_migration', 'Rank',
                'Learner', '', 'Migration test', 'quick', '', '[]',
                'high', 'confirmed', '{timestamp}'
            );
            INSERT INTO series (id, plan_id, shelf_id, title, rationale)
            VALUES (
                'series_rank_migration', 'plan_rank_migration',
                'shelf_rank_migration', 'Rank', 'Migration test'
            );
            INSERT INTO books (
                id, series_id, shelf_id, position, title, topic,
                description, estimated_minutes
            ) VALUES (
                'book_rank_migration', 'series_rank_migration',
                'shelf_rank_migration', 1, 'Rank', 'Rank', 'Migration test', 20
            );
            INSERT INTO chapters (id, book_id, position, title, objective)
            VALUES (
                'chapter_rank_migration', 'book_rank_migration', 1,
                'Rank', 'Migration test'
            );
            INSERT INTO sections (
                id, chapter_id, position, title, question, objectives_json
            ) VALUES (
                'section_rank_migration', 'chapter_rank_migration', 1,
                'Rank', 'Migration test?', '["Migration test"]'
            );
            INSERT INTO learning_runs (
                id, user_id, series_id, status, created_at
            ) VALUES (
                'run_rank_migration', 'user_rank_migration',
                'series_rank_migration', 'active', '{timestamp}'
            );
            INSERT INTO assessment_targets (
                id, objective_key, objective_statement, dimension,
                target_depth, status, created_at, identity_status
            ) VALUES (
                'target_rank_migration', 'rank-migration', 'Migration test',
                'application', 'standard', 'active', '{timestamp}',
                'legacy_provisional'
            );
            """
        )
        connection.execute(
            """
            INSERT INTO assessment_observations (
                id, learning_run_id, user_id, section_id,
                assessment_target_id, correct, source_type,
                assistance_mode, learning_episode_id, equivalence_group_id,
                qualification_at_creation, qualification_rule_version,
                payload_json, created_at
            ) VALUES (
                'observation_repeat_v2', 'run_rank_migration',
                'user_rank_migration', 'section_rank_migration',
                'target_rank_migration', 1, 'choice_quiz',
                'unassisted_repeat', 'episode_repeat', 'same-question',
                'eligible_grouped', 'evidence_v2', '{}', ?
            )
            """,
            (timestamp,),
        )
        connection.executemany(
            """
            INSERT INTO evidence_qualification_events (
                id, observation_id, projection_family, status,
                reason, rule_version, created_at
            ) VALUES (?, 'observation_repeat_v2', ?, ?, ?, 'evidence_v2', ?)
            """,
            [
                (
                    "qualification_repeat_gate_v2",
                    "gate",
                    "eligible",
                    "historical gate",
                    timestamp,
                ),
                (
                    "qualification_repeat_mastery_v2",
                    "mastery",
                    "eligible_grouped",
                    "historical mastery",
                    timestamp,
                ),
                (
                    "qualification_repeat_retention_v2",
                    "retention",
                    "ineligible",
                    "historical retention",
                    timestamp,
                ),
            ],
        )
        connection.commit()

    run_alembic(database, "upgrade", "head")

    with sqlite3.connect(database) as connection:
        v2_count = connection.execute(
            "SELECT COUNT(*) FROM evidence_qualification_events "
            "WHERE observation_id='observation_repeat_v2' "
            "AND rule_version='evidence_v2'"
        ).fetchone()[0]
        v3 = dict(
            connection.execute(
                "SELECT projection_family, status "
                "FROM evidence_qualification_events "
                "WHERE observation_id='observation_repeat_v2' "
                "AND rule_version='evidence_v3'"
            ).fetchall()
        )
    assert v2_count == 3
    assert v3 == {
        "gate": "eligible",
        "mastery": "ineligible",
        "retention": "ineligible",
        "rank": "ineligible",
    }

    run_alembic(database, "downgrade", "0054_m3_pilot_foundations")
    with sqlite3.connect(database) as connection:
        v3_count = connection.execute(
            "SELECT COUNT(*) FROM evidence_qualification_events "
            "WHERE rule_version='evidence_v3'"
        ).fetchone()[0]
        node_table = connection.execute(
            "SELECT COUNT(*) FROM sqlite_master "
            "WHERE type='table' AND name='knowledge_node_state_projections'"
        ).fetchone()[0]
    assert v3_count == 0
    assert node_table == 0


def test_0048_downgrade_refuses_oral_assessment_facts(tmp_path):
    database = tmp_path / "0048-oral-facts.db"
    run_alembic(database, "upgrade", "0048_ask_me_evidence_and_turn_leases")
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute(
            """
            INSERT INTO assessment_observations (
                id, learning_run_id, user_id, section_id,
                learning_contract_version_id, assessment_target_id, correct,
                source_type, evidence_key, assistance_mode,
                learning_episode_id, equivalence_group_id,
                qualification_at_creation, qualification_rule_version,
                payload_json, created_at
            ) VALUES (
                'observation_oral', 'run_missing', 'user_missing',
                'section_missing', 'contract_missing', 'target_missing', 1,
                'ask_me_topic', 'oral-evidence-key', 'unassisted_oral',
                'ask_me_topic:topic_missing', 'oral-equivalence',
                'eligible_grouped', 'evidence_v2', '{}',
                '2026-08-09 12:00:00'
            )
            """
        )
        connection.commit()

    with pytest.raises(subprocess.CalledProcessError):
        run_alembic(
            database,
            "downgrade",
            "0047_historical_schema_repair",
        )

    with sqlite3.connect(database) as connection:
        revision = connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()[0]
        oral_count = connection.execute(
            "SELECT COUNT(*) FROM assessment_observations "
            "WHERE source_type = 'ask_me_topic'"
        ).fetchone()[0]
    assert revision == "0048_ask_me_evidence_and_turn_leases"
    assert oral_count == 1


def test_0048_downgrade_refuses_duplicate_discussion_retries(tmp_path):
    database = tmp_path / "0048-discussion-retries.db"
    run_alembic(database, "upgrade", "0048_ask_me_evidence_and_turn_leases")
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.executemany(
            """
            INSERT INTO ask_me_discussion_turns (
                id, session_id, topic_id, user_id, turn_index,
                prompt, answer, evaluation, feedback_json, status,
                idempotency_key, request_hash, response_json, error_code,
                lease_token, lease_expires_at, created_at, updated_at
            ) VALUES (?, 'session_missing', 'topic_missing', 'user_missing', 0,
                'prompt', 'answer', ?, '{}', ?, ?, ?, '', ?, '', NULL,
                '2026-08-09 12:00:00', '2026-08-09 12:00:00')
            """,
            [
                (
                    "turn_failed",
                    "",
                    "failed",
                    "retry-key-failed",
                    "hash-failed",
                    "ASK_ME_DISCUSSION_AI_FAILED",
                ),
                (
                    "turn_completed",
                    "strong",
                    "completed",
                    "retry-key-completed",
                    "hash-completed",
                    "",
                ),
            ],
        )
        connection.commit()

    with pytest.raises(subprocess.CalledProcessError):
        run_alembic(
            database,
            "downgrade",
            "0047_historical_schema_repair",
        )

    with sqlite3.connect(database) as connection:
        revision = connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()[0]
        retry_count = connection.execute(
            "SELECT COUNT(*) FROM ask_me_discussion_turns "
            "WHERE topic_id = 'topic_missing' AND turn_index = 0"
        ).fetchone()[0]
    assert revision == "0048_ask_me_evidence_and_turn_leases"
    assert retry_count == 2


def test_0049_downgrade_refuses_account_recovery_history(tmp_path):
    database = tmp_path / "0049-recovery-history.db"
    run_alembic(database, "upgrade", "head")
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute(
            """
            INSERT INTO account_recovery_codes (
                id, user_id, version, code_hash, status, failed_attempts,
                created_at
            ) VALUES (
                'recovery_history', 'user_missing', 1,
                'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
                'used', 0, '2026-08-11 12:00:00'
            )
            """
        )
        connection.commit()

    with pytest.raises(subprocess.CalledProcessError):
        run_alembic(
            database,
            "downgrade",
            "0048_ask_me_evidence_and_turn_leases",
        )

    with sqlite3.connect(database) as connection:
        revision = connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()[0]
        recovery_count = connection.execute(
            "SELECT COUNT(*) FROM account_recovery_codes"
        ).fetchone()[0]
    # 0050 has no protected history and downgrades first; 0049 then refuses to
    # discard the recovery record and leaves the database at that revision.
    assert revision == "0049_alpha_account_recovery"
    assert recovery_count == 1


def test_generation_lease_migration_accepts_orm_precreated_table(tmp_path):
    database = tmp_path / "precreated-lease.db"
    run_alembic(database, "upgrade", "0015_initial_lesson_preload")
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            CREATE TABLE generation_leases (
                resource_key VARCHAR(200) NOT NULL PRIMARY KEY,
                owner_id VARCHAR(80) NOT NULL UNIQUE,
                acquired_at DATETIME NOT NULL,
                expires_at DATETIME NOT NULL
            )
            """
        )
        connection.execute(
            "CREATE INDEX ix_generation_leases_expires_at "
            "ON generation_leases (expires_at)"
        )

    run_alembic(database, "upgrade", "head")
    with sqlite3.connect(database) as connection:
        revision = connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()[0]
    assert revision == HEAD_REVISION


def test_layered_note_migration_preserves_legacy_ai_and_user_content(tmp_path):
    database = tmp_path / "legacy-notes.db"
    run_alembic(database, "upgrade", "0027_milestone_paths")
    ai_content = {
        "solved_question": "旧 AI 总结",
        "core_mechanism": ["稳定底稿不能丢"],
    }
    user_content = {
        "solved_question": "用户自己的表述",
        "boundaries": ["不能被 AI 覆盖"],
    }
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO users (id, name) VALUES (?, ?)",
            ("legacy_user", "Legacy"),
        )
        connection.execute(
            """
            INSERT INTO shelves (
                id, user_id, name, domain, specialty, tags_json, origin
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "legacy_shelf",
                "legacy_user",
                "迁移书架",
                "技术",
                "",
                "[]",
                "user_created",
            ),
        )
        connection.execute(
            """
            INSERT INTO learning_plans (
                id, shelf_id, topic, role, experience, purpose, depth,
                details, assumptions_json, confidence, status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "legacy_plan",
                "legacy_shelf",
                "迁移",
                "学习者",
                "",
                "",
                "overview",
                "",
                "[]",
                "high",
                "active",
                "2026-08-01 07:00:00",
            ),
        )
        connection.execute(
            """
            INSERT INTO series (
                id, plan_id, shelf_id, title, rationale, deleted_at
            ) VALUES (?, ?, ?, ?, ?, NULL)
            """,
            (
                "legacy_series",
                "legacy_plan",
                "legacy_shelf",
                "迁移系列",
                "验证笔记迁移",
            ),
        )
        connection.execute(
            """
            INSERT INTO books (
                id, series_id, shelf_id, position, title, topic,
                description, estimated_minutes, deleted_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)
            """,
            (
                "legacy_book",
                "legacy_series",
                "legacy_shelf",
                1,
                "迁移书",
                "迁移",
                "",
                20,
            ),
        )
        connection.execute(
            """
            INSERT INTO chapters (
                id, book_id, position, title, objective
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                "legacy_chapter",
                "legacy_book",
                1,
                "迁移章",
                "验证笔记升级",
            ),
        )
        connection.executemany(
            """
            INSERT INTO sections (
                id, chapter_id, position, title, question, objectives_json
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    "legacy_section",
                    "legacy_chapter",
                    1,
                    "迁移节一",
                    "旧笔记是否保留？",
                    "[]",
                ),
                (
                    "legacy_section_2",
                    "legacy_chapter",
                    2,
                    "迁移节二",
                    "空用户版本是否跳过？",
                    "[]",
                ),
            ],
        )
        connection.execute(
            """
            INSERT INTO learning_runs (
                id, user_id, series_id, status, created_at, completed_at
            ) VALUES (?, ?, ?, ?, ?, NULL)
            """,
            (
                "legacy_run",
                "legacy_user",
                "legacy_series",
                "active",
                "2026-08-01 07:30:00",
            ),
        )
        connection.execute(
            """
            INSERT INTO section_progress (
                id, learning_run_id, user_id, section_id, status,
                best_score, total_score, ask_me_unlocked, version, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "legacy_section_progress",
                "legacy_run",
                "legacy_user",
                "legacy_section",
                "completed",
                5,
                5,
                1,
                3,
                "2026-08-01 08:00:00",
            ),
        )
        connection.executemany(
            """
            INSERT INTO learning_notes (
                id, learning_run_id, section_id, user_id, ai_content_json,
                user_content_json, version, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    "legacy_note_with_user",
                    "legacy_run",
                    "legacy_section",
                    "legacy_user",
                    json.dumps(ai_content, ensure_ascii=False),
                    json.dumps(user_content, ensure_ascii=False),
                    4,
                    "2026-08-01 08:00:00",
                ),
                (
                    "legacy_note_without_user",
                    "legacy_run",
                    "legacy_section_2",
                    "legacy_user",
                    json.dumps({"solved_question": "只有 AI"}, ensure_ascii=False),
                    "{}",
                    1,
                    "2026-08-01 09:00:00",
                ),
            ],
        )
        connection.commit()

    run_alembic(database, "upgrade", "head")

    with sqlite3.connect(database) as connection:
        summaries = connection.execute(
            """
            SELECT note_id, content_json, source_contract_version,
                   generation_rule_version
            FROM learning_note_summaries
            ORDER BY note_id
            """
        ).fetchall()
        revisions = connection.execute(
            """
            SELECT note_id, content_json, source
            FROM learning_note_user_revisions
            ORDER BY note_id
            """
        ).fetchall()
        mission = connection.execute(
            """
            SELECT mission.id, mission.status, mission.why,
                   series.initial_mission_version_id,
                   run.initial_mission_version_id
            FROM learning_mission_versions AS mission
            JOIN series ON series.plan_id = mission.plan_id
            JOIN learning_runs AS run ON run.series_id = series.id
            WHERE series.id = 'legacy_series'
            """
        ).fetchone()
        adoption = connection.execute(
            """
            SELECT source, event_type
            FROM mission_adoption_events
            WHERE learning_run_id = 'legacy_run'
            """
        ).fetchone()
        progress = connection.execute(
            """
            SELECT id, status, best_score, total_score, ask_me_unlocked, version
            FROM section_progress
            WHERE id = 'legacy_section_progress'
            """
        ).fetchone()

    assert [row[0] for row in summaries] == [
        "legacy_note_with_user",
        "legacy_note_without_user",
    ]
    assert json.loads(summaries[0][1]) == ai_content
    assert all(row[2] == "legacy_learning_note_v1" for row in summaries)
    assert all(row[3] == "legacy_note_import_v1" for row in summaries)
    assert len(revisions) == 1
    assert revisions[0][0] == "legacy_note_with_user"
    assert json.loads(revisions[0][1]) == user_content
    assert revisions[0][2] == "legacy_user_import_v1"
    assert mission[1:] == (
        "grandfathered_m1",
        "建立系统认知与基础实践准备",
        mission[0],
        mission[0],
    )
    assert adoption == ("system_migration", "initialized")
    assert progress == (
        "legacy_section_progress",
        "completed",
        5,
        5,
        1,
        3,
    )


def test_migrations_recover_when_orm_precreated_future_tables(tmp_path):
    database = tmp_path / "orm-precreated.db"
    run_alembic(database, "upgrade", "0018_multi_user_state_authority")

    engine = create_engine(f"sqlite+pysqlite:///{database}")
    Base.metadata.create_all(engine)
    engine.dispose()

    run_alembic(database, "upgrade", "head")

    with sqlite3.connect(database) as connection:
        revision = connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()[0]
        task_columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(learning_tasks)")
        }
        foreign_key_errors = connection.execute(
            "PRAGMA foreign_key_check"
        ).fetchall()

    assert revision == HEAD_REVISION
    assert {
        "lease_owner",
        "lease_token",
        "lease_expires_at",
        "heartbeat_at",
    }.issubset(task_columns)
    assert foreign_key_errors == []


def test_layered_note_migration_repairs_intermediate_summary_schema(tmp_path):
    database = tmp_path / "intermediate-note-summary.db"
    run_alembic(database, "upgrade", "0027_milestone_paths")

    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            CREATE TABLE learning_note_summaries (
                id VARCHAR NOT NULL PRIMARY KEY,
                note_id VARCHAR NOT NULL,
                version INTEGER NOT NULL,
                content_json TEXT NOT NULL,
                source_content_version_id VARCHAR,
                source_observation_watermark INTEGER NOT NULL DEFAULT 0,
                generation_rule_version VARCHAR(40) NOT NULL
                    DEFAULT 'note_summary_v1',
                created_at DATETIME NOT NULL,
                UNIQUE (note_id, version),
                FOREIGN KEY(note_id) REFERENCES learning_notes (id),
                FOREIGN KEY(source_content_version_id)
                    REFERENCES content_versions (id)
            )
            """
        )

    run_alembic(database, "upgrade", "head")

    with sqlite3.connect(database) as connection:
        revision = connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()[0]
        summary_columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(learning_note_summaries)"
            )
        }

    assert revision == HEAD_REVISION
    assert "source_contract_version" in summary_columns


def test_assessment_migration_repairs_section_scoped_target_schema(tmp_path):
    database = tmp_path / "section-scoped-targets.db"
    run_alembic(database, "upgrade", "0027_milestone_paths")

    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            CREATE TABLE assessment_targets (
                id VARCHAR NOT NULL PRIMARY KEY,
                section_id VARCHAR NOT NULL,
                objective_key VARCHAR(300) NOT NULL,
                objective_statement TEXT NOT NULL,
                dimension VARCHAR(32) NOT NULL,
                target_depth VARCHAR(32) NOT NULL,
                status VARCHAR(24) NOT NULL,
                created_at DATETIME NOT NULL,
                CONSTRAINT uq_assessment_targets_section_semantics
                    UNIQUE (
                        section_id, objective_key, dimension, target_depth
                    ),
                FOREIGN KEY(section_id) REFERENCES sections (id)
            )
            """
        )
        connection.execute(
            "CREATE INDEX ix_assessment_targets_section_id "
            "ON assessment_targets (section_id)"
        )
        connection.execute(
            "CREATE INDEX ix_assessment_targets_status "
            "ON assessment_targets (status)"
        )

    run_alembic(database, "upgrade", "head")

    with sqlite3.connect(database) as connection:
        revision = connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()[0]
        target_columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(assessment_targets)"
            )
        }

    assert revision == HEAD_REVISION
    assert "section_id" not in target_columns


def test_historical_schema_repair_restores_known_intermediate_shapes(tmp_path):
    database = tmp_path / "historical-intermediate-shapes.db"
    run_alembic(database, "upgrade", "0046_daily_mode")

    projection_fragment = (
        "\n\tprojection_version INTEGER DEFAULT '1' NOT NULL, ",
    )
    evidence_constraint = (
        ", \n\tCONSTRAINT "
        "uq_evidence_qualification_observation_family_rule "
        "UNIQUE (observation_id, projection_family, rule_version)"
    )
    feedback_fragments = (
        "\n\tidempotency_key VARCHAR(128) NOT NULL, ",
        "\n\trequest_hash VARCHAR(64) NOT NULL, ",
        ", \n\tCONSTRAINT uq_user_feedback_user_idempotency "
        "UNIQUE (user_id, idempotency_key)",
    )

    with sqlite3.connect(database) as connection:
        for table_name in (
            "assessment_gate_states",
            "knowledge_state_projections",
            "review_states",
        ):
            recreate_sqlite_table_without(
                connection,
                table_name,
                projection_fragment,
            )
        recreate_sqlite_table_without(
            connection,
            "evidence_qualification_events",
            (evidence_constraint,),
        )
        recreate_sqlite_table_without(
            connection,
            "user_feedback",
            feedback_fragments,
        )
        connection.execute(
            "INSERT INTO users (id, name) VALUES (?, ?)",
            ("historical_feedback_user", "Historical Feedback User"),
        )
        user_id = connection.execute(
            "SELECT id FROM users ORDER BY id LIMIT 1"
        ).fetchone()[0]
        connection.execute(
            """
            INSERT INTO user_feedback (
                id, user_id, scope, feedback_type, message, page_path,
                view, section_id, content_version_id, block_id,
                block_snapshot_hash, source_mode, schema_version,
                context_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "feedback_historical",
                user_id,
                "global",
                "other",
                "保留这条历史反馈",
                "/",
                "library",
                None,
                None,
                None,
                "",
                "legacy_intermediate",
                "feedback_v1",
                "{}",
                "2026-08-05 20:00:00",
            ),
        )
        connection.commit()

    run_alembic(database, "upgrade", "head")

    with sqlite3.connect(database) as connection:
        revision = connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()[0]
        projection_columns = {
            table_name: {
                row[1]: row
                for row in connection.execute(
                    f'PRAGMA table_info("{table_name}")'
                )
            }
            for table_name in (
                "assessment_gate_states",
                "knowledge_state_projections",
                "review_states",
            )
        }
        evidence_schema = connection.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type = 'table' "
            "AND name = 'evidence_qualification_events'"
        ).fetchone()[0]
        feedback_schema = connection.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type = 'table' AND name = 'user_feedback'"
        ).fetchone()[0]
        feedback = connection.execute(
            "SELECT message, idempotency_key, request_hash "
            "FROM user_feedback WHERE id = 'feedback_historical'"
        ).fetchone()
        foreign_key_errors = connection.execute(
            "PRAGMA foreign_key_check"
        ).fetchall()

    assert revision == HEAD_REVISION
    for columns in projection_columns.values():
        assert columns["projection_version"][3] == 1
        assert columns["projection_version"][4] == "'1'"
    assert (
        "uq_evidence_qualification_observation_family_rule"
        in evidence_schema
    )
    assert "uq_user_feedback_user_idempotency" in feedback_schema
    assert feedback[0] == "保留这条历史反馈"
    assert feedback[1].startswith("historical:")
    assert len(feedback[2]) == 64
    assert foreign_key_errors == []

    # Downgrading this repair must not remove invariants that were already
    # authoritative at 0046. A subsequent upgrade is therefore a no-op.
    run_alembic(database, "downgrade", "0046_daily_mode")
    run_alembic(database, "upgrade", "head")


def test_historical_schema_repair_refuses_duplicate_immutable_facts(tmp_path):
    database = tmp_path / "historical-duplicate-evidence.db"
    run_alembic(database, "upgrade", "0046_daily_mode")
    evidence_constraint = (
        ", \n\tCONSTRAINT "
        "uq_evidence_qualification_observation_family_rule "
        "UNIQUE (observation_id, projection_family, rule_version)"
    )

    with sqlite3.connect(database) as connection:
        recreate_sqlite_table_without(
            connection,
            "evidence_qualification_events",
            (evidence_constraint,),
        )
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.executemany(
            """
            INSERT INTO evidence_qualification_events (
                id, observation_id, projection_family, status, reason,
                rule_version, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    "qualification_duplicate_1",
                    "missing_observation",
                    "mastery",
                    "eligible",
                    "historical duplicate",
                    "evidence_v1",
                    "2026-08-05 20:00:00",
                ),
                (
                    "qualification_duplicate_2",
                    "missing_observation",
                    "mastery",
                    "eligible",
                    "historical duplicate",
                    "evidence_v1",
                    "2026-08-05 20:01:00",
                ),
            ],
        )
        connection.commit()

    with pytest.raises(subprocess.CalledProcessError):
        run_alembic(database, "upgrade", "head")

    with sqlite3.connect(database) as connection:
        revision = connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()[0]
        count = connection.execute(
            "SELECT COUNT(*) FROM evidence_qualification_events"
        ).fetchone()[0]

    assert revision == "0046_daily_mode"
    assert count == 2


def test_shelf_origin_migration_removes_only_empty_non_demo_defaults(tmp_path):
    database = tmp_path / "default-shelf-cleanup.db"
    run_alembic(database, "upgrade", "0025_profile_onboarding")

    with sqlite3.connect(database) as connection:
        connection.executemany(
            "INSERT INTO users (id, name) VALUES (?, ?)",
            [
                ("user_empty", "Empty"),
                ("user_retained", "Retained"),
                ("user_demo", "Demo"),
            ],
        )
        connection.executemany(
            """
            INSERT INTO shelves (
                id, user_id, name, domain, specialty, tags_json
            ) VALUES (?, ?, '技术', '计算机', '软件工程', '["AI","云原生"]')
            """,
            [
                ("shelf_empty", "user_empty"),
                ("shelf_retained", "user_retained"),
                ("shelf_demo", "user_demo"),
            ],
        )
        connection.execute(
            """
            INSERT INTO learning_plans (
                id, shelf_id, topic, role, experience, purpose, depth,
                details, assumptions_json, confidence, status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "plan_retained",
                "shelf_retained",
                "已开始的学习",
                "学习者",
                "已有记录",
                "继续学习",
                "overview",
                "",
                "[]",
                "high",
                "active",
                "2026-08-03 00:00:00",
            ),
        )
        connection.commit()

    run_alembic(database, "upgrade", "head")

    with sqlite3.connect(database) as connection:
        rows = connection.execute(
            "SELECT id, origin FROM shelves ORDER BY id"
        ).fetchall()

    assert rows == [
        ("shelf_demo", "demo_seed"),
        ("shelf_retained", "legacy_auto_seed"),
    ]


def test_populated_0014_database_upgrades_without_losing_user_facts(tmp_path):
    database = tmp_path / "legacy.db"
    run_alembic(database, "upgrade", "0014_durable_learning_tasks")

    timestamp = "2026-07-31 08:00:00"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO users (id, name) VALUES (?, ?)",
            ("legacy_user", "Legacy"),
        )
        connection.execute(
            """
            INSERT INTO shelves (
                id, user_id, name, domain, specialty, tags_json
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            ("legacy_shelf", "legacy_user", "技术", "计算机", "", "[]"),
        )
        connection.execute(
            """
            INSERT INTO learning_plans (
                id, shelf_id, topic, role, experience, purpose, depth,
                details, assumptions_json, confidence, status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "legacy_plan",
                "legacy_shelf",
                "迁移",
                "学习者",
                "",
                "",
                "overview",
                "",
                "[]",
                "high",
                "active",
                timestamp,
            ),
        )
        connection.execute(
            """
            INSERT INTO series (
                id, plan_id, shelf_id, title, rationale, deleted_at
            ) VALUES (?, ?, ?, ?, ?, NULL)
            """,
            (
                "legacy_series",
                "legacy_plan",
                "legacy_shelf",
                "迁移系列",
                "验证升级",
            ),
        )
        connection.execute(
            """
            INSERT INTO books (
                id, series_id, shelf_id, position, title, topic,
                description, estimated_minutes, status, deleted_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
            """,
            (
                "legacy_book",
                "legacy_series",
                "legacy_shelf",
                1,
                "迁移书",
                "迁移",
                "",
                20,
                "available",
            ),
        )
        connection.execute(
            """
            INSERT INTO chapters (
                id, book_id, position, title, objective, status
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                "legacy_chapter",
                "legacy_book",
                1,
                "迁移章",
                "验证升级",
                "available",
            ),
        )
        connection.execute(
            """
            INSERT INTO sections (
                id, chapter_id, position, title, question,
                objectives_json, status, best_score, total_score,
                ask_me_unlocked
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "legacy_section",
                "legacy_chapter",
                1,
                "迁移节",
                "升级后是否还在？",
                "[]",
                "completed",
                3,
                3,
                1,
            ),
        )
        connection.execute(
            """
            INSERT INTO learning_runs (
                id, user_id, series_id, status, created_at, completed_at
            ) VALUES (?, ?, ?, ?, ?, NULL)
            """,
            (
                "legacy_run",
                "legacy_user",
                "legacy_series",
                "active",
                timestamp,
            ),
        )
        connection.execute(
            """
            INSERT INTO artifact_progress (
                id, learning_run_id, user_id, target_type, target_id,
                status, submission_json, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "legacy_artifact",
                "legacy_run",
                "legacy_user",
                "chapter_practice",
                "legacy_practice",
                "completed",
                json.dumps(
                    {
                        "content": {"answer": "保留我"},
                        "attachmentIds": ["attachment_1"],
                    },
                    ensure_ascii=False,
                ),
                timestamp,
            ),
        )
        connection.execute(
            """
            INSERT INTO learning_tasks (
                id, learning_run_id, user_id, section_id, task_type,
                idempotency_key, trigger_id, payload_json, result_json,
                status, attempt_count, max_attempts, error_code,
                error_message, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "legacy_task",
                "legacy_run",
                "legacy_user",
                "legacy_section",
                "note_generation",
                "legacy-key",
                "legacy-trigger",
                "{}",
                "{}",
                "running",
                1,
                3,
                "",
                "",
                timestamp,
                timestamp,
            ),
        )
        connection.execute(
            """
            INSERT INTO plan_creation_requests (
                idempotency_key, user_id, request_hash, status, series_id,
                error_code, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "legacy-request",
                "legacy_user",
                "a" * 64,
                "succeeded",
                "legacy_series",
                "",
                timestamp,
                timestamp,
            ),
        )

    run_alembic(database, "upgrade", "head")

    with sqlite3.connect(database) as connection:
        user = connection.execute(
            "SELECT name, status FROM users WHERE id = 'legacy_user'"
        ).fetchone()
        section = connection.execute(
            "SELECT title FROM sections WHERE id = 'legacy_section'"
        ).fetchone()
        section_columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(sections)")
        }
        request = connection.execute(
            """
            SELECT user_id, series_id
            FROM plan_creation_requests
            WHERE idempotency_key = 'legacy-request'
            """
        ).fetchone()
        lease_expires_at = connection.execute(
            """
            SELECT lease_expires_at
            FROM learning_tasks
            WHERE id = 'legacy_task'
            """
        ).fetchone()[0]
        submission = connection.execute(
            """
            SELECT content_json, attachment_ids_json
            FROM artifact_submissions
            WHERE target_id = 'legacy_practice'
            """
        ).fetchone()
        foreign_key_errors = connection.execute(
            "PRAGMA foreign_key_check"
        ).fetchall()

    assert user == ("Legacy", "active")
    assert section == ("迁移节",)
    assert {
        "status",
        "best_score",
        "total_score",
        "ask_me_unlocked",
    }.isdisjoint(section_columns)
    assert request == ("legacy_user", "legacy_series")
    assert lease_expires_at is not None
    assert json.loads(submission[0]) == {"answer": "保留我"}
    assert json.loads(submission[1]) == ["attachment_1"]
    assert foreign_key_errors == []
