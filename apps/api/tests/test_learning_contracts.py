import hashlib
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.infrastructure.tables import (
    AssessmentTarget,
    Base,
    Book,
    Chapter,
    Concept,
    ConceptRevision,
    LearningContractAssessmentTarget,
    LearningContractVersion,
    LearningMissionVersion,
    LearningObjective,
    LearningPlan,
    Section,
    SectionAssessmentTarget,
    Series,
    Shelf,
    User,
)
from app.modules.learning.contracts import ensure_m1_learning_contract


API_ROOT = Path(__file__).resolve().parents[1]


def _db() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Session(engine)


def _seed_section(db: Session, *, with_target: bool = True) -> tuple[Section, str | None]:
    db.add(User(id="user_contract", name="Contract Learner"))
    db.flush()
    db.add(
        Shelf(
            id="shelf_contract",
            user_id="user_contract",
            name="计算机",
            domain="计算机",
            specialty="",
            tags_json="[]",
        )
    )
    db.flush()
    db.add(
        LearningPlan(
            id="plan_contract",
            shelf_id="shelf_contract",
            topic="测试",
            role="学生",
            experience="",
            purpose="测试迁移",
            depth="deep",
            details="",
            assumptions_json="[]",
            confidence="high",
            status="active",
        )
    )
    db.flush()
    mission = LearningMissionVersion(
        id="mission_contract",
        plan_id="plan_contract",
        user_id="user_contract",
        version=1,
        status="grandfathered_m1",
        why="测试迁移",
        target_capabilities_json="[]",
        constraints_json="{}",
        out_of_scope_json="[]",
        assumptions_json="[]",
        learner_context_json="{}",
        inferred_fields_json="[]",
        provenance_json="{}",
        schema_version="mission_v1",
        payload_hash="a" * 64,
    )
    db.add(mission)
    db.flush()
    db.add(
        Series(
            id="series_contract",
            plan_id="plan_contract",
            shelf_id="shelf_contract",
            title="测试",
            rationale="测试",
            initial_mission_version_id=mission.id,
        )
    )
    db.flush()
    db.add(
        Book(
            id="book_contract",
            series_id="series_contract",
            shelf_id="shelf_contract",
            position=1,
            title="测试书",
            topic="测试",
            description="",
            estimated_minutes=20,
        )
    )
    db.flush()
    db.add(
        Chapter(
            id="chapter_contract",
            book_id="book_contract",
            position=1,
            title="测试章",
            objective="测试目标",
        )
    )
    db.flush()
    section = Section(
        id="section_contract",
        chapter_id="chapter_contract",
        position=1,
        title="测试节",
        question="为什么需要契约？",
        objectives_json='["解释契约身份", "识别契约边界"]',
    )
    db.add(section)
    db.flush()
    if not with_target:
        return section, None

    key = hashlib.sha256("解释契约身份".encode()).hexdigest()
    target = AssessmentTarget(
        id="target_keep_me",
        objective_key=key,
        objective_statement="解释契约身份",
        dimension="recognition",
        target_depth="standard",
        status="active",
    )
    db.add(target)
    db.flush()
    db.add(
        SectionAssessmentTarget(
            id="section_target_keep_me",
            section_id=section.id,
            assessment_target_id=target.id,
            position=1,
            required=True,
            verification_policy="choice_quiz_v1",
        )
    )
    db.flush()
    return section, target.id


def test_m1_contract_preserves_target_identity_and_is_idempotent():
    db = _db()
    section, target_id = _seed_section(db)

    first = ensure_m1_learning_contract(db, section)
    second = ensure_m1_learning_contract(db, section)
    target = db.get(AssessmentTarget, target_id)

    assert first.id == second.id
    assert target.id == "target_keep_me"
    assert target.identity_status == "legacy_provisional"
    assert target.concept_revision_id
    assert target.learning_objective_id
    revision = db.get(ConceptRevision, target.concept_revision_id)
    assert revision is not None
    assert db.get(Concept, revision.concept_id).origin == "m1_provisional"
    assert db.scalar(select(func.count()).select_from(LearningContractVersion)) == 1
    binding = db.scalar(select(LearningContractAssessmentTarget))
    assert binding.assessment_target_id == target.id
    assert binding.required is True


def test_unmaterialized_m1_section_gets_deterministic_provisional_targets():
    db = _db()
    section, _ = _seed_section(db, with_target=False)

    contract = ensure_m1_learning_contract(db, section)
    targets = db.scalars(select(AssessmentTarget).order_by(AssessmentTarget.id)).all()
    contract_targets = db.scalars(
        select(LearningContractAssessmentTarget)
        .where(LearningContractAssessmentTarget.contract_version_id == contract.id)
        .order_by(LearningContractAssessmentTarget.position)
    ).all()

    assert len(targets) == 2
    assert len(contract_targets) == 2
    assert contract_targets[0].required is True
    assert contract_targets[1].required is False
    assert all(item.concept_revision_id for item in targets)
    assert all(item.learning_objective_id for item in targets)
    assert db.scalar(select(func.count()).select_from(LearningObjective)) == 2


def test_0030_upgrades_populated_0029_without_changing_target_id(tmp_path):
    database = tmp_path / "contracts-0029.db"
    environment = {
        **os.environ,
        "DATABASE_URL": f"sqlite+pysqlite:///{database}",
        "PYTHONPATH": ".",
    }
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "0029_learning_missions"],
        cwd=API_ROOT,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )

    timestamp = "2026-08-04 00:00:00"
    key = hashlib.sha256("核心目标".encode()).hexdigest()
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
            (timestamp,),
        )
        connection.execute(
            "INSERT INTO learning_mission_versions(id,plan_id,user_id,version,status,why,"
            "target_capabilities_json,constraints_json,out_of_scope_json,assumptions_json,"
            "learner_context_json,inferred_fields_json,provenance_json,schema_version,"
            "payload_hash,supersedes_id,confirmed_at,created_at) VALUES"
            "('m','p','u',1,'grandfathered_m1','P','[]','{}','[]','[]','{}','[]','{}',"
            "'mission_v1',?,NULL,NULL,?)",
            ("b" * 64, timestamp),
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
            "VALUES('c','b',1,'C','O')"
        )
        connection.execute(
            "INSERT INTO sections(id,chapter_id,position,title,question,objectives_json) "
            "VALUES('sec','c',1,'Sec','Q','[\"核心目标\"]')"
        )
        connection.execute(
            "INSERT INTO assessment_targets(id,objective_key,objective_statement,dimension,"
            "target_depth,status,created_at) VALUES"
            "('target_original',?,'核心目标','recognition','standard','active',?)",
            (key, timestamp),
        )
        connection.execute(
            "INSERT INTO section_assessment_targets(id,section_id,assessment_target_id,"
            "position,required,verification_policy,created_at) VALUES"
            "('st','sec','target_original',1,1,'choice_quiz_v1',?)",
            (timestamp,),
        )
        connection.commit()

    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=API_ROOT,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    with sqlite3.connect(database) as connection:
        target = connection.execute(
            "SELECT id, concept_revision_id, learning_objective_id, identity_status "
            "FROM assessment_targets"
        ).fetchone()
        contract_target = connection.execute(
            "SELECT assessment_target_id, required "
            "FROM learning_contract_assessment_targets"
        ).fetchone()

    assert target[0] == "target_original"
    assert target[1]
    assert target[2]
    assert target[3] == "legacy_provisional"
    assert contract_target == ("target_original", 1)
