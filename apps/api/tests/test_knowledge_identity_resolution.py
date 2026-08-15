import json

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.infrastructure.tables import (
    AssessmentTarget,
    Base,
    CapabilityRevision,
    CapabilityRouteBinding,
    CapabilityStageCriterion,
    Concept,
    KnowledgeIdentityCandidate,
    KnowledgeIdentityDecision,
)
from app.modules.knowledge.identity import materialize_candidate_target


def _db() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Session(engine)


def _candidate(*, definition: str = "递归通过缩小问题并抵达基本情形完成求解") -> dict:
    return {
        "candidate_key": "recursion",
        "label": "递归",
        "definition": definition,
        "scope": "解释终止机制、适用边界并迁移到搜索与动态规划",
        "boundaries": ["循环不是递归", "支撑知识不自动成为考核目标"],
        "reuse_concept_revision_id": "",
    }


def test_exact_candidate_reuses_revision_while_targets_keep_dimensions():
    db = _db()

    mechanism = materialize_candidate_target(
        db,
        series_id="series_a",
        section_id="section_book_1",
        statement="解释递归为什么能够终止",
        dimension="mechanism",
        candidate=_candidate(),
    )
    transfer = materialize_candidate_target(
        db,
        series_id="series_a",
        section_id="section_book_2",
        statement="把递归迁移到深度优先搜索",
        dimension="transfer",
        candidate=_candidate(),
    )
    db.commit()

    assert mechanism.id != transfer.id
    assert mechanism.dimension == "mechanism"
    assert transfer.dimension == "transfer"
    assert mechanism.concept_revision_id == transfer.concept_revision_id
    assert mechanism.capability_revision_id == transfer.capability_revision_id
    assert mechanism.capability_stage_criterion_id == (
        transfer.capability_stage_criterion_id
    )
    assert db.scalar(select(func.count()).select_from(Concept)) == 1
    assert db.scalar(select(func.count()).select_from(CapabilityRevision)) == 1
    assert db.scalar(select(func.count()).select_from(CapabilityRouteBinding)) == 1
    bronze = db.get(
        CapabilityStageCriterion,
        mechanism.capability_stage_criterion_id,
    )
    assert bronze.stage == "bronze"
    assert bronze.verification_protocol == "choice_quiz_v1"
    decisions = db.scalars(
        select(KnowledgeIdentityDecision).order_by(
            KnowledgeIdentityDecision.created_at,
            KnowledgeIdentityDecision.id,
        )
    ).all()
    assert {item.decision for item in decisions} == {
        "create_concept",
        "reuse_revision",
    }


def test_same_name_with_different_meaning_stays_separate_and_unresolved():
    db = _db()
    first = materialize_candidate_target(
        db,
        series_id="series_a",
        section_id="section_1",
        statement="解释递归的终止机制",
        dimension="mechanism",
        candidate=_candidate(),
    )
    second = materialize_candidate_target(
        db,
        series_id="series_a",
        section_id="section_2",
        statement="辨认语言学中的递归结构",
        dimension="recognition",
        candidate=_candidate(definition="递归是句法结构嵌套同类结构的性质"),
    )
    db.commit()

    assert first.concept_revision_id != second.concept_revision_id
    assert db.scalar(select(func.count()).select_from(Concept)) == 2
    unresolved = db.scalar(
        select(KnowledgeIdentityDecision).where(
            KnowledgeIdentityDecision.decision == "unresolved"
        )
    )
    assert unresolved is not None
    assert first.concept_revision_id in json.loads(
        unresolved.compared_revision_ids_json
    )
    candidate = db.get(KnowledgeIdentityCandidate, unresolved.candidate_id)
    assert candidate.status == "unresolved"


def test_unpublished_candidate_does_not_merge_across_series():
    db = _db()
    first = materialize_candidate_target(
        db,
        series_id="series_a",
        section_id="section_a",
        statement="解释递归终止",
        dimension="mechanism",
        candidate=_candidate(),
    )
    second = materialize_candidate_target(
        db,
        series_id="series_b",
        section_id="section_b",
        statement="解释递归终止",
        dimension="mechanism",
        candidate=_candidate(),
    )

    assert first.concept_revision_id != second.concept_revision_id
    assert db.scalar(select(func.count()).select_from(Concept)) == 2


def test_unallowlisted_revision_reference_fails_closed():
    db = _db()
    candidate = _candidate()
    candidate["reuse_concept_revision_id"] = "concept_revision_not_allowed"

    with pytest.raises(AppError) as error:
        materialize_candidate_target(
            db,
            series_id="series_a",
            section_id="section_1",
            statement="解释递归",
            dimension="mechanism",
            candidate=candidate,
        )

    assert error.value.code == "KNOWLEDGE_IDENTITY_REUSE_NOT_ALLOWED"


def test_invalid_capability_dimension_fails_closed():
    db = _db()

    with pytest.raises(AppError) as error:
        materialize_candidate_target(
            db,
            series_id="series_a",
            section_id="section_1",
            statement="解释递归",
            dimension="difficulty",
            candidate=_candidate(),
        )

    assert error.value.code == "KNOWLEDGE_CAPABILITY_DIMENSION_INVALID"
