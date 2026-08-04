import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

from sqlalchemy import create_engine

from app.infrastructure.tables import Base


API_ROOT = Path(__file__).resolve().parents[1]
HEAD_REVISION = "0028_trustworthy_assessment_notes"


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
        assessment_target_columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(assessment_targets)")
        }
        gate_columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(assessment_gate_states)")
        }
        knowledge_columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(knowledge_state_projections)")
        }
        review_columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(review_states)")
        }

    assert revision == HEAD_REVISION
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
        "review_states",
        "learning_note_summaries",
        "learning_note_review_supplements",
        "learning_note_user_revisions",
    }.issubset(trustworthy_tables)
    assert "section_id" not in assessment_target_columns
    assert "projection_version" in gate_columns
    assert "projection_version" in knowledge_columns
    assert "projection_version" in review_columns
    assert any(
        row[1] == "sqlite_autoindex_milestone_path_revisions_2" or row[2] == 1
        for row in milestone_revision_indexes
    )


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
