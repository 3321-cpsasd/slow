import json
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


def _insert_minimal_content_graph(connection: sqlite3.Connection) -> None:
    timestamp = "2026-08-04 00:00:00"
    connection.execute("INSERT INTO users(id,name) VALUES('u','U')")
    connection.execute(
        "INSERT INTO shelves(id,user_id,name,domain,specialty,tags_json,origin) "
        "VALUES('s','u','S','D','','[]','user_created')"
    )
    connection.execute(
        "INSERT INTO learning_plans(id,shelf_id,topic,role,experience,purpose,depth,"
        "details,assumptions_json,confidence,status,created_at) VALUES"
        "('p','s','T','R','E','P','deep','','[]','high','active',?)",
        (timestamp,),
    )
    connection.execute(
        "INSERT INTO learning_mission_versions(id,plan_id,user_id,version,status,why,"
        "target_capabilities_json,constraints_json,out_of_scope_json,assumptions_json,"
        "learner_context_json,inferred_fields_json,provenance_json,schema_version,"
        "payload_hash,supersedes_id,confirmed_at,created_at) VALUES"
        "('m','p','u',1,'grandfathered_m1','P','[]','{}','[]','[]','{}','[]','{}',"
        "'mission_v1',?,NULL,NULL,?)",
        ("c" * 64, timestamp),
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
    connection.execute(
        "INSERT INTO sections(id,chapter_id,position,title,question,objectives_json) "
        "VALUES('sec','ch',1,'S','Q','[]')"
    )
    connection.execute(
        "INSERT INTO learning_contract_versions(id,section_id,mission_version_id,version,"
        "section_question_snapshot,target_depth,boundaries_json,generation_context_json,"
        "provenance_mode,lineage_status,contract_hash,created_at) VALUES"
        "('lc','sec','m',1,'Q','standard','[]','{}','derived_from_m1','provisional',?,?)",
        ("d" * 64, timestamp),
    )
    blocks = [
        {
            "id": "legacy_conclusion",
            "version": 3,
            "kind": "text",
            "role": "conclusion",
            "heading": "结论",
            "content": "核心事实原文",
            "source_indexes": [0],
        },
        {
            "id": "legacy_mechanism",
            "kind": "text",
            "role": "mechanism",
            "heading": "机制",
            "content": "普通教学解释",
            "source_indexes": [0],
        },
        {
            "id": "legacy_example",
            "kind": "text",
            "role": "example",
            "heading": "例子",
            "content": "普通例子",
            "source_indexes": [0],
        },
        {
            "id": "legacy_boundary",
            "kind": "text",
            "role": "boundary",
            "heading": "边界",
            "content": "关键边界",
            "source_indexes": [0],
        },
        {
            "id": "legacy_assessable",
            "kind": "text",
            "role": "mechanism",
            "heading": "可考",
            "content": "明确可考事实",
            "source_indexes": [0],
            "assessmentEligible": True,
        },
        {
            "id": "legacy_versioned",
            "kind": "text",
            "role": "example",
            "heading": "版本",
            "content": "版本敏感事实",
            "source_indexes": [0],
            "factualityClass": "version_sensitive_fact",
        },
        {
            "id": "legacy_relation",
            "kind": "text",
            "role": "mechanism",
            "heading": "关系",
            "content": "关键知识关系",
            "source_indexes": [0],
            "knowledgeRelation": {"type": "requires"},
        },
    ]
    sources = [
        {
            "title": "Reachable does not mean supporting",
            "url": "https://example.com/reference",
            "kind": "official",
            "version": "2026-08",
        }
    ]
    connection.execute(
        "INSERT INTO content_versions(id,section_id,learning_contract_version_id,version,"
        "blocks_json,sources_json,confidence,created_at) VALUES"
        "('content','sec','lc',1,?,?, 'high',?)",
        (json.dumps(blocks, ensure_ascii=False), json.dumps(sources), timestamp),
    )
    connection.execute(
        "INSERT INTO source_verifications(id,content_version_id,report_json,verified_at) "
        "VALUES('verification','content',?,?)",
        (
            json.dumps(
                [
                    {
                        "url": "https://example.com/reference",
                        "reachable": True,
                        "statusCode": 200,
                        "pinned": True,
                        "verificationStatus": "verified",
                    }
                ]
            ),
            timestamp,
        ),
    )
    connection.commit()


def test_0032_normalizes_m1_without_promoting_reachability_to_support(tmp_path):
    database = tmp_path / "governance-backfill.db"
    _alembic(database, "upgrade", "0031_learning_version_bindings")
    with sqlite3.connect(database) as connection:
        _insert_minimal_content_graph(connection)

    _alembic(database, "upgrade", "0032_content_governance")
    with sqlite3.connect(database) as connection:
        revision = connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()[0]
        blocks = connection.execute(
            "SELECT id, block_version, semantic_role, heading, content, trust_state, "
            "generation_method FROM content_block_versions ORDER BY position"
        ).fetchall()
        source = connection.execute(
            "SELECT reachability_status, provenance_mode FROM source_versions"
        ).fetchone()
        claims = connection.execute(
            "SELECT anchor.content_block_version_id, claim.claim_kind, claim.trust_state, "
            "claim.status FROM source_claim_versions AS claim JOIN content_block_claim_anchors "
            "AS anchor ON anchor.source_claim_version_id=claim.id "
            "ORDER BY anchor.content_block_version_id"
        ).fetchall()
        bindings = connection.execute(
            "SELECT binding.support_type, binding.verification_mode, "
            "binding.verification_status, binding.verified_at, binding.locator_type, "
            "binding.report_json FROM source_claim_bindings AS binding"
        ).fetchall()
        gaps = connection.execute(
            "SELECT gap.gap_type, gap.severity, event.event_type "
            "FROM knowledge_gaps AS gap JOIN knowledge_gap_events AS event "
            "ON event.knowledge_gap_id=gap.id"
        ).fetchall()
        foreign_key_errors = connection.execute(
            "PRAGMA foreign_key_check"
        ).fetchall()

    assert revision == "0032_content_governance"
    assert [row[0] for row in blocks] == [
        "legacy_conclusion",
        "legacy_mechanism",
        "legacy_example",
        "legacy_boundary",
        "legacy_assessable",
        "legacy_versioned",
        "legacy_relation",
    ]
    assert blocks[0][1:5] == (3, "conclusion", "结论", "核心事实原文")
    assert blocks[1][4:7] == (
        "普通教学解释",
        "model_synthesis",
        "derived_from_m1",
    )
    assert blocks[2][5] == "model_synthesis"
    assert blocks[0][5] == blocks[3][5] == "legacy_unverified"
    assert source == ("verified", "derived_from_m1")
    assert {row[0] for row in claims} == {
        "legacy_conclusion",
        "legacy_boundary",
        "legacy_assessable",
        "legacy_versioned",
        "legacy_relation",
    }
    assert {row[1] for row in claims} == {
        "core_conclusion",
        "boundary",
        "assessable_fact",
        "version_sensitive_fact",
        "key_knowledge_relation",
    }
    assert all(row[2:] == ("legacy_unverified", "candidate") for row in claims)
    assert len(bindings) == 5
    assert all(row[:5] == (
        "candidate_support",
        "legacy_migration",
        "legacy_unverified",
        None,
        "legacy_source_index",
    ) for row in bindings)
    assert all(
        json.loads(row[5])["warning"]
        == "source_reachability_is_not_claim_support"
        for row in bindings
    )
    assert gaps == [("unsupported_claim", "blocking", "opened")] * 5
    assert foreign_key_errors == []


def test_0032_fresh_schema_has_governance_foreign_keys(tmp_path):
    database = tmp_path / "governance-fresh.db"
    _alembic(database, "upgrade", "head")
    with sqlite3.connect(database) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        foreign_key_errors = connection.execute(
            "PRAGMA foreign_key_check"
        ).fetchall()
        binding_fks = {
            row[2]
            for row in connection.execute(
                "PRAGMA foreign_key_list(source_claim_bindings)"
            )
        }
    assert {
        "source_versions",
        "content_block_versions",
        "source_claims",
        "source_claim_versions",
        "content_block_claim_anchors",
        "source_claim_bindings",
        "knowledge_gaps",
        "knowledge_gap_events",
        "governance_decision_snapshots",
    }.issubset(tables)
    assert binding_fks == {"source_claim_versions", "source_versions"}
    assert foreign_key_errors == []
