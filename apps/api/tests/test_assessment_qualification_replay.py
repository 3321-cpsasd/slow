import json
from datetime import timedelta

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.infrastructure.tables import (
    AssessmentGateState,
    AssessmentObservation,
    AssessmentTarget,
    Base,
    EvidenceQualificationEvent,
    KnowledgeStateProjection,
    SectionAssessmentTarget,
    now,
)
from app.modules.learning.assessment import (
    QUALIFICATION_RULE_VERSION,
    rebuild_assessment_projections,
)


def _session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Session(engine)


def _add_target(db: Session, target_id: str = "target_replay") -> None:
    db.add(
        AssessmentTarget(
            id=target_id,
            objective_key=f"key_{target_id}",
            objective_statement="解释资格事件如何约束证据重放",
            dimension="recognition",
            target_depth="standard",
            status="active",
        )
    )
    db.add(
        SectionAssessmentTarget(
            id=f"binding_{target_id}",
            section_id="section_replay",
            assessment_target_id=target_id,
            position=1,
            required=True,
            verification_policy="choice_quiz_v1",
        )
    )


def _add_observation(
    db: Session,
    *,
    suffix: str,
    created_at,
    correct: bool,
    assistance_mode: str,
    gate_status: str = "eligible",
    mastery_status: str = "eligible_grouped",
    retention_status: str = "ineligible",
    target_id: str = "target_replay",
) -> AssessmentObservation:
    observation = AssessmentObservation(
        id=f"observation_{suffix}",
        learning_run_id="run_replay",
        user_id="user_replay",
        section_id="section_replay",
        attempt_id=f"attempt_{suffix}",
        scoring_result_id=f"scoring_{suffix}",
        assessment_target_id=target_id,
        question_index=0,
        correct=correct,
        assistance_mode=assistance_mode,
        learning_episode_id=f"episode_{suffix}",
        equivalence_group_id=f"equivalence_{suffix}",
        qualification_at_creation=mastery_status,
        qualification_rule_version=QUALIFICATION_RULE_VERSION,
        payload_json=json.dumps({"questionFingerprint": f"fingerprint_{suffix}"}),
        created_at=created_at,
    )
    db.add(observation)
    db.flush()
    for family, status in {
        "gate": gate_status,
        "mastery": mastery_status,
        "retention": retention_status,
    }.items():
        db.add(
            EvidenceQualificationEvent(
                id=f"qualification_{suffix}_{family}_{QUALIFICATION_RULE_VERSION}",
                observation_id=observation.id,
                projection_family=family,
                status=status,
                reason="test qualification",
                rule_version=QUALIFICATION_RULE_VERSION,
                created_at=created_at,
            )
        )
    return observation


def test_replay_uses_family_qualification_and_repeat_is_not_retention() -> None:
    with _session() as db:
        _add_target(db)
        started_at = now()
        initial = _add_observation(
            db,
            suffix="initial",
            created_at=started_at,
            correct=True,
            assistance_mode="unassisted_initial",
        )
        repeated = _add_observation(
            db,
            suffix="repeat",
            created_at=started_at + timedelta(days=2),
            correct=True,
            assistance_mode="unassisted_repeat",
        )
        # An older rule considered the repeat a retention candidate. Replay of
        # the selected current rule must not accidentally consume it.
        db.add(
            EvidenceQualificationEvent(
                id="qualification_repeat_retention_evidence_v1",
                observation_id=repeated.id,
                projection_family="retention",
                status="candidate",
                reason="legacy rule",
                rule_version="evidence_v1",
                created_at=started_at + timedelta(days=2),
            )
        )
        db.flush()

        report = rebuild_assessment_projections(
            db,
            user_id="user_replay",
            qualification_rule_version=QUALIFICATION_RULE_VERSION,
        )
        state = db.scalar(select(KnowledgeStateProjection))
        gate = db.scalar(select(AssessmentGateState))
        assert report["qualifiedGateObservations"] == 2
        assert report["qualifiedMasteryObservations"] == 2
        assert report["qualifiedRetentionObservations"] == 0
        assert state.retention_rounds == 0
        assert state.claim_status == "verified_immediate"
        assert gate.status == "resolved_initial"
        assert gate.resolved_by_observation_id == initial.id

        _add_observation(
            db,
            suffix="assigned_review",
            created_at=started_at + timedelta(days=4),
            correct=True,
            assistance_mode="unassisted_review",
            retention_status="candidate",
        )
        db.flush()
        report = rebuild_assessment_projections(db, user_id="user_replay")
        db.refresh(state)
        assert report["qualifiedRetentionObservations"] == 1
        assert state.retention_rounds == 1
        assert state.claim_status == "verified_delayed"


def test_ineligible_family_event_is_not_consumed_by_projection() -> None:
    with _session() as db:
        _add_target(db)
        started_at = now()
        eligible = _add_observation(
            db,
            suffix="eligible",
            created_at=started_at,
            correct=True,
            assistance_mode="unassisted_initial",
        )
        _add_observation(
            db,
            suffix="blocked",
            created_at=started_at + timedelta(days=1),
            correct=False,
            assistance_mode="unassisted_initial",
            gate_status="ineligible_gap",
            mastery_status="ineligible_gap",
            retention_status="ineligible_gap",
        )
        db.flush()

        report = rebuild_assessment_projections(db, user_id="user_replay")
        state = db.scalar(select(KnowledgeStateProjection))
        gate = db.scalar(select(AssessmentGateState))
        assert report["observations"] == 2
        assert report["qualifiedGateObservations"] == 1
        assert report["qualifiedMasteryObservations"] == 1
        assert report["qualifiedRetentionObservations"] == 0
        assert state.source_observation_watermark == eligible.sequence
        assert state.claim_status == "verified_immediate"
        assert gate.status == "resolved_initial"
