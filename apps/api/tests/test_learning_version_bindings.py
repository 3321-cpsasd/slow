import os
from pathlib import Path
import sqlite3
import subprocess
import sys


API_ROOT = Path(__file__).resolve().parents[1]


def _alembic(database: Path, *arguments: str) -> None:
    subprocess.run(
        [sys.executable, "-m", "alembic", *arguments],
        cwd=API_ROOT,
        env={
            **os.environ,
            "DATABASE_URL": f"sqlite+pysqlite:///{database}",
            "PYTHONPATH": ".",
        },
        check=True,
        capture_output=True,
        text=True,
    )


def test_0031_binds_real_activity_but_not_preloaded_content(tmp_path):
    database = tmp_path / "version-bindings.db"
    _alembic(database, "upgrade", "0030_learning_contracts")
    t0 = "2026-08-04 00:00:00"
    with sqlite3.connect(database) as connection:
        connection.execute("INSERT INTO users(id,name) VALUES('u','U')")
        connection.execute(
            "INSERT INTO shelves(id,user_id,name,domain,specialty,tags_json,origin) "
            "VALUES('s','u','S','D','','[]','user_created')"
        )
        connection.execute(
            "INSERT INTO learning_plans(id,shelf_id,topic,role,experience,purpose,depth,"
            "details,assumptions_json,confidence,status,created_at) VALUES"
            "('p','s','T','R','E','P','deep','','[]','high','active',?)",
            (t0,),
        )
        connection.execute(
            "INSERT INTO learning_mission_versions(id,plan_id,user_id,version,status,why,"
            "target_capabilities_json,constraints_json,out_of_scope_json,assumptions_json,"
            "learner_context_json,inferred_fields_json,provenance_json,schema_version,"
            "payload_hash,supersedes_id,confirmed_at,created_at) VALUES"
            "('m','p','u',1,'grandfathered_m1','P','[]','{}','[]','[]','{}','[]','{}',"
            "'mission_v1',?,NULL,NULL,?)",
            ("c" * 64, t0),
        )
        connection.execute(
            "INSERT INTO series(id,plan_id,shelf_id,title,rationale,deleted_at,"
            "initial_mission_version_id) VALUES('ser','p','s','S','R',NULL,'m')"
        )
        connection.execute(
            "INSERT INTO books(id,series_id,shelf_id,position,title,topic,description,"
            "estimated_minutes,deleted_at) VALUES('b','ser','s',1,'B','T','',20,NULL)"
        )
        connection.execute(
            "INSERT INTO chapters(id,book_id,position,title,objective) "
            "VALUES('ch','b',1,'C','O')"
        )
        connection.executemany(
            "INSERT INTO sections(id,chapter_id,position,title,question,objectives_json) "
            "VALUES(?,?,?,?,?,'[]')",
            [
                ("learned", "ch", 1, "Learned", "Q1"),
                ("preloaded", "ch", 2, "Preloaded", "Q2"),
            ],
        )
        connection.executemany(
            "INSERT INTO learning_contract_versions(id,section_id,mission_version_id,version,"
            "section_question_snapshot,target_depth,boundaries_json,generation_context_json,"
            "provenance_mode,lineage_status,contract_hash,created_at) "
            "VALUES(?,?, 'm',1,?,'standard','[]','{}','derived_from_m1','provisional',?,?)",
            [
                ("lc1", "learned", "Q1", "1" * 64, t0),
                ("lc2", "preloaded", "Q2", "2" * 64, t0),
            ],
        )
        connection.executemany(
            "INSERT INTO content_versions(id,section_id,version,blocks_json,sources_json,"
            "confidence,created_at) VALUES(?,?,?,?, '[]','high',?)",
            [
                ("content_old", "learned", 1, '[{"id":"block_old"}]', "2026-08-04 00:10:00"),
                ("content_new", "learned", 2, '[{"id":"block_new"}]', "2026-08-04 01:30:00"),
                ("content_preload", "preloaded", 1, '[{"id":"block_preload"}]', "2026-08-04 00:20:00"),
            ],
        )
        connection.executemany(
            "INSERT INTO quiz_sets(id,section_id,content_version_id,generation,questions_json) "
            "VALUES(?,?,?,?, '[]')",
            [
                ("quiz_old", "learned", "content_old", 1),
                ("quiz_new", "learned", "content_new", 2),
                ("quiz_preload", "preloaded", "content_preload", 1),
            ],
        )
        connection.execute(
            "INSERT INTO learning_runs(id,user_id,series_id,initial_mission_version_id,status,"
            "created_at,completed_at) VALUES('run','u','ser','m','active',?,NULL)",
            (t0,),
        )
        connection.execute(
            "INSERT INTO qa_sessions(id,learning_run_id,section_id,user_id,memory_json) "
            "VALUES('qa','run','learned','u','{}')"
        )
        connection.execute(
            "INSERT INTO qa_messages(id,session_id,thread_id,block_id,role,content,created_at) "
            "VALUES('msg','qa','thread','block_old','user','question','2026-08-04 01:00:00')"
        )
        connection.execute(
            "INSERT INTO quiz_attempts(id,quiz_set_id,learning_run_id,user_id,idempotency_key,"
            "request_hash,answers_json,results_json,passed,workflow_status,response_json,"
            "workflow_error_code,created_at) VALUES"
            "('attempt','quiz_new','run','u','key','hash','[]','[]',1,'succeeded','{}','',"
            "'2026-08-04 02:00:00')"
        )
        connection.commit()

    _alembic(database, "upgrade", "0031_learning_version_bindings")
    with sqlite3.connect(database) as connection:
        revision = connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()[0]
        contents = connection.execute(
            "SELECT id, learning_contract_version_id FROM content_versions ORDER BY id"
        ).fetchall()
        attempt = connection.execute(
            "SELECT content_version_id, learning_contract_version_id FROM quiz_attempts"
        ).fetchone()
        qa = connection.execute(
            "SELECT content_version_id, learning_contract_version_id FROM qa_sessions"
        ).fetchone()
        bindings = connection.execute(
            "SELECT section_id, content_version_id, source, source_fact_id "
            "FROM learning_run_section_bindings"
        ).fetchall()
        fk_errors = connection.execute("PRAGMA foreign_key_check").fetchall()

    assert revision == "0031_learning_version_bindings"
    assert contents == [
        ("content_new", "lc1"),
        ("content_old", "lc1"),
        ("content_preload", "lc2"),
    ]
    assert attempt == ("content_new", "lc1")
    assert qa == ("content_old", "lc1")
    assert bindings == [("learned", "content_new", "attempt", "attempt")]
    assert fk_errors == []
