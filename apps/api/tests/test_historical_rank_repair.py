import json

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.infrastructure.tables import (
    AssessmentObservation,
    AssessmentTarget,
    AssessmentTargetRankIdentityDecision,
    Base,
    Book,
    Chapter,
    ContentVersion,
    EvidenceQualificationEvent,
    KnowledgeNodeStateProjection,
    KnowledgeStateProjection,
    LearningPlan,
    LearningContractAssessmentTarget,
    LearningContractVersion,
    QuizSet,
    Section,
    SectionAssessmentTarget,
    Series,
    Shelf,
    User,
    now,
)
from app.modules.learning.assessment import QUALIFICATION_RULE_VERSION
from app.modules.learning.historical_rank_repair import (
    repair_published_historical_rank_identities,
)
from app.modules.learning.knowledge_map import KnowledgeMapService


def _session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Session(engine)


def _seed_published_legacy_lesson(
    db: Session,
    *,
    bind_contract: bool = True,
    quiz_has_target: bool = True,
    include_observation: bool = True,
) -> None:
    db.add(User(id="user_history", name="历史学习者"))
    db.add(
        Shelf(
            id="shelf_history",
            user_id="user_history",
            name="历史书架",
            domain="测试",
        )
    )
    db.add(
        LearningPlan(
            id="plan_history",
            shelf_id="shelf_history",
            topic="历史证据",
            role="学习者",
            experience="",
            depth="deep",
            confidence="high",
            status="active",
        )
    )
    db.add(
        Series(
            id="series_history",
            plan_id="plan_history",
            shelf_id="shelf_history",
            title="历史系列",
            rationale="验证历史能力迁移",
        )
    )
    db.add(
        Book(
            id="book_history",
            series_id="series_history",
            shelf_id="shelf_history",
            position=1,
            title="历史教材",
            topic="历史教材",
            description="用于验证精确身份迁移",
            estimated_minutes=60,
        )
    )
    db.add(
        Chapter(
            id="chapter_history",
            book_id="book_history",
            position=1,
            title="第一章",
            objective="理解历史能力证据",
        )
    )
    db.add(
        Section(
            id="section_history",
            chapter_id="chapter_history",
            position=1,
            title="历史小节",
            question="历史证据如何进入段位？",
            objectives_json='["解释历史证据如何进入段位"]',
        )
    )
    db.add(
        AssessmentTarget(
            id="target_history",
            objective_key="legacy-history",
            objective_statement="解释历史证据如何进入段位",
            dimension="recognition",
            target_depth="standard",
            identity_status="legacy_provisional",
            status="active",
        )
    )
    db.add(
        SectionAssessmentTarget(
            id="section_target_history",
            section_id="section_history",
            assessment_target_id="target_history",
            position=1,
            required=True,
            verification_policy="choice_quiz_v1",
        )
    )
    db.add(
        LearningContractVersion(
            id="contract_history",
            section_id="section_history",
            mission_version_id="mission_history",
            version=1,
            section_question_snapshot="历史证据如何进入段位？",
            target_depth="standard",
            contract_hash="c" * 64,
        )
    )
    if bind_contract:
        db.add(
            LearningContractAssessmentTarget(
                id="contract_target_history",
                contract_version_id="contract_history",
                assessment_target_id="target_history",
                position=1,
                required=True,
                verification_policy="choice_quiz_v1",
                evidence_policy="assessment_evidence_v1",
                diagnostic_only=False,
            )
        )
    db.add(
        ContentVersion(
            id="content_history",
            section_id="section_history",
            learning_contract_version_id="contract_history",
            version=1,
            blocks_json="[]",
            sources_json="[]",
            confidence="medium",
            publication_status="published",
        )
    )
    db.add(
        QuizSet(
            id="quiz_history",
            section_id="section_history",
            content_version_id="content_history",
            learning_contract_version_id="contract_history",
            generation=1,
            questions_json=json.dumps(
                [
                    {"assessmentTargetId": "target_history"}
                    if quiz_has_target
                    else {"prompt": "没有能力目标的旧题"}
                ]
            ),
            publication_status="published",
        )
    )
    if not include_observation:
        db.flush()
        return
    observation = AssessmentObservation(
        id="observation_history",
        learning_run_id="run_history",
        user_id="user_history",
        section_id="section_history",
        attempt_id="attempt_history",
        quiz_set_id="quiz_history",
        learning_contract_version_id="contract_history",
        content_version_id="content_history",
        scoring_result_id="scoring_history",
        assessment_target_id="target_history",
        question_index=0,
        correct=True,
        assistance_mode="unassisted_initial",
        learning_episode_id="episode_history",
        equivalence_group_id="equivalence_history",
        qualification_at_creation="eligible_grouped",
        qualification_rule_version=QUALIFICATION_RULE_VERSION,
        payload_json='{"dimension":"recognition"}',
        created_at=now(),
    )
    db.add(observation)
    db.flush()
    for family, status in {
        "gate": "eligible",
        "mastery": "eligible_grouped",
        "retention": "ineligible",
        "rank": "eligible_grouped",
    }.items():
        db.add(
            EvidenceQualificationEvent(
                id=f"qualification_history_{family}",
                observation_id=observation.id,
                projection_family=family,
                status=status,
                reason="test qualification",
                rule_version=QUALIFICATION_RULE_VERSION,
                created_at=observation.created_at,
            )
        )
    db.flush()


def test_repair_adds_identity_decision_and_replays_without_rewriting_facts() -> None:
    with _session() as db:
        _seed_published_legacy_lesson(db)

        report = repair_published_historical_rank_identities(db)
        observation = db.scalar(
            select(AssessmentObservation).where(
                AssessmentObservation.id == "observation_history"
            )
        )
        source = db.get(AssessmentTarget, "target_history")
        decision = db.scalar(select(AssessmentTargetRankIdentityDecision))
        target_state = db.scalar(select(KnowledgeStateProjection))
        node_state = db.scalar(select(KnowledgeNodeStateProjection))

        assert report["identityDecisionsCreated"] == 1
        assert report["affectedLearners"] == 1
        assert observation.assessment_target_id == "target_history"
        assert source.identity_status == "legacy_provisional"
        assert decision.source_assessment_target_id == "target_history"
        assert decision.destination_assessment_target_id != "target_history"
        assert target_state.assessment_target_id == "target_history"
        assert node_state.current_rank == "bronze"
        knowledge_map = KnowledgeMapService(
            db, user_id="user_history"
        ).view(series_id="series_history")
        assert knowledge_map["availability"] == "ready"
        assert knowledge_map["progress"]["verifiedTargets"] == 1
        assert knowledge_map["progress"]["requiredTargets"] == 1
        assert knowledge_map["nodes"][0]["rank"] == "bronze"

        repeated = repair_published_historical_rank_identities(db)
        assert repeated["identityDecisionsCreated"] == 0
        assert repeated["alreadySettleablePairs"] == 1
        assert len(db.scalars(select(AssessmentTargetRankIdentityDecision)).all()) == 1


def test_repair_fails_closed_when_quiz_target_is_not_contract_bound() -> None:
    with _session() as db:
        _seed_published_legacy_lesson(db, bind_contract=False)

        with pytest.raises(AppError) as error:
            repair_published_historical_rank_identities(db)

        assert error.value.code == "HISTORICAL_RANK_REPAIR_TARGET_UNBOUND"
        assert db.scalar(select(AssessmentTargetRankIdentityDecision)) is None


def test_repair_audits_but_skips_unobserved_quiz_without_target_ids() -> None:
    with _session() as db:
        _seed_published_legacy_lesson(
            db,
            quiz_has_target=False,
            include_observation=False,
        )

        report = repair_published_historical_rank_identities(db)

        assert report["eligibleQuizSets"] == 0
        assert report["legacyQuizSetsSkipped"] == 1
        assert report["identityDecisionsCreated"] == 0


def test_repair_refuses_observed_quiz_without_target_ids() -> None:
    with _session() as db:
        _seed_published_legacy_lesson(db, quiz_has_target=False)

        with pytest.raises(AppError) as error:
            repair_published_historical_rank_identities(db)

        assert error.value.code == (
            "HISTORICAL_RANK_REPAIR_EVIDENCE_QUIZ_INVALID"
        )
