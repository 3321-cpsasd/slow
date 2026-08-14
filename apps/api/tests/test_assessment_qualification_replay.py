import json
from datetime import timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.infrastructure.tables import (
    AssessmentGateState,
    AssessmentObservation,
    AssessmentTarget,
    AssessmentTargetRankIdentityDecision,
    Base,
    Concept,
    ConceptRevision,
    EvidenceQualificationEvent,
    KnowledgeNodeStateProjection,
    KnowledgeStateProjection,
    LearnerKnowledgeProfileProjection,
    SectionAssessmentTarget,
    now,
)
from app.modules.learning.assessment import (
    QUALIFICATION_RULE_VERSION,
    rebuild_assessment_projections,
)
from app.modules.learning.knowledge_ranks import (
    knowledge_node_views_for_targets,
    knowledge_settlement,
    require_effective_rank_targets,
)


def _session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Session(engine)


def _add_target(
    db: Session,
    target_id: str = "target_replay",
    *,
    dimension: str = "recognition",
    published_concept: bool = False,
    rank_policy: bool = True,
    rank_ceiling: str = "gold",
    bind_section: bool = True,
) -> None:
    concept_revision_id = None
    identity_status = "legacy_provisional"
    if published_concept:
        concept_id = f"concept_{target_id}"
        concept_revision_id = f"concept_revision_{target_id}"
        identity_status = "published_knowledge_graph"
        db.add(
            Concept(
                id=concept_id,
                namespace="test",
                concept_key=target_id,
                canonical_name="导数的最优化应用",
                status="active",
                origin="test",
            )
        )
        db.add(
            ConceptRevision(
                id=concept_revision_id,
                concept_id=concept_id,
                revision=1,
                label="导数的最优化应用",
                definition="使用导数寻找约束下的极值。",
                scope_json=json.dumps(
                    {
                        "rankPolicy": {
                            "version": "knowledge_rank_policy_v1",
                            "capabilityScope": "独立解决单变量标准最优化问题",
                            "rankCeiling": rank_ceiling,
                            "dimensionRanks": {dimension: rank_ceiling},
                        }
                    }
                    if rank_policy
                    else {}
                ),
                verification_status="reviewed",
            )
        )
    db.add(
        AssessmentTarget(
            id=target_id,
            concept_revision_id=concept_revision_id,
            objective_key=f"key_{target_id}",
            objective_statement="解释资格事件如何约束证据重放",
            dimension=dimension,
            target_depth="standard",
            identity_status=identity_status,
            status="active",
        )
    )
    if bind_section:
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
    rank_status: str = "eligible_grouped",
    target_id: str = "target_replay",
    episode_id: str | None = None,
    learning_contract_version_id: str | None = None,
) -> AssessmentObservation:
    observation = AssessmentObservation(
        id=f"observation_{suffix}",
        learning_run_id="run_replay",
        user_id="user_replay",
        section_id="section_replay",
        attempt_id=f"attempt_{suffix}",
        scoring_result_id=f"scoring_{suffix}",
        learning_contract_version_id=learning_contract_version_id,
        assessment_target_id=target_id,
        question_index=0,
        correct=correct,
        assistance_mode=assistance_mode,
        learning_episode_id=episode_id or f"episode_{suffix}",
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
        "rank": rank_status,
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
            mastery_status="ineligible",
            rank_status="ineligible",
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
        profile = db.scalar(select(LearnerKnowledgeProfileProjection))
        gate = db.scalar(select(AssessmentGateState))
        assert report["qualifiedGateObservations"] == 2
        assert report["qualifiedMasteryObservations"] == 1
        assert report["qualifiedRetentionObservations"] == 0
        assert state.retention_rounds == 0
        assert state.claim_status == "verified_immediate"
        assert json.loads(profile.summary_json)["rankedNodeCount"] == 0
        assert profile.source_observation_watermark == repeated.sequence
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


def test_published_node_rank_groups_one_quiz_as_one_independent_star() -> None:
    with _session() as db:
        _add_target(
            db,
            target_id="target_application",
            dimension="application",
            published_concept=True,
        )
        started_at = now()
        for suffix in ("q1", "q2", "q3"):
            _add_observation(
                db,
                suffix=suffix,
                created_at=started_at,
                correct=True,
                assistance_mode="unassisted_initial",
                target_id="target_application",
                episode_id="quiz:one-attempt",
            )
        db.flush()

        report = rebuild_assessment_projections(db, user_id="user_replay")
        state = db.scalar(select(KnowledgeNodeStateProjection))

        assert report["knowledgeNodeStates"] == 1
        assert report["qualifiedRankObservations"] == 3
        assert state.current_rank == "gold"
        assert state.current_stars == 1
        assert state.highest_rank == "gold"
        assert state.independent_evidence_count == 1
        assert state.activation_state == "active"

        views = knowledge_node_views_for_targets(
            db,
            user_id="user_replay",
            target_ids={"target_application"},
        )
        first_settlement = knowledge_settlement({}, views)
        assert first_settlement["updates"][0]["change"] == "rank_up"
        assert first_settlement["updates"][0]["before"]["rank"] == "unranked"
        assert first_settlement["updates"][0]["after"]["rankLabel"] == "黄金 · 会用"
        assert first_settlement["updates"][0]["after"]["atCeiling"] is True
        assert first_settlement["updates"][0]["after"]["capabilityScope"] == (
            "独立解决单变量标准最优化问题"
        )
        repeated_settlement = knowledge_settlement(views, views)
        assert repeated_settlement["updates"][0]["change"] == "confirmed"


def test_published_node_without_explicit_local_rank_policy_fails_closed() -> None:
    with _session() as db:
        _add_target(
            db,
            target_id="target_without_policy",
            dimension="application",
            published_concept=True,
            rank_policy=False,
        )
        _add_observation(
            db,
            suffix="without_policy",
            created_at=now(),
            correct=True,
            assistance_mode="unassisted_initial",
            target_id="target_without_policy",
        )
        db.flush()

        report = rebuild_assessment_projections(db, user_id="user_replay")

        assert report["qualifiedRankObservations"] == 1
        assert report["knowledgeNodeStates"] == 0
        assert db.scalar(select(KnowledgeNodeStateProjection)) is None
        assert knowledge_node_views_for_targets(
            db,
            user_id="user_replay",
            target_ids={"target_without_policy"},
        ) == {}


def test_repeat_cannot_farm_rank_and_fresh_failure_preserves_highest_rank() -> None:
    with _session() as db:
        _add_target(
            db,
            target_id="target_application",
            dimension="application",
            published_concept=True,
        )
        started_at = now()
        _add_observation(
            db,
            suffix="initial",
            created_at=started_at,
            correct=True,
            assistance_mode="unassisted_initial",
            target_id="target_application",
        )
        _add_observation(
            db,
            suffix="repeat",
            created_at=started_at + timedelta(hours=1),
            correct=True,
            assistance_mode="unassisted_repeat",
            target_id="target_application",
            mastery_status="ineligible",
            rank_status="ineligible",
        )
        db.flush()
        rebuild_assessment_projections(db, user_id="user_replay")
        state = db.scalar(select(KnowledgeNodeStateProjection))
        assert state.current_rank == "gold"
        assert state.current_stars == 1
        assert state.evidence_count == 1

        failed = _add_observation(
            db,
            suffix="fresh_failure",
            created_at=started_at + timedelta(days=3),
            correct=False,
            assistance_mode="unassisted_review",
            target_id="target_application",
            retention_status="candidate",
        )
        db.flush()
        rebuild_assessment_projections(db, user_id="user_replay")
        db.refresh(state)

        assert state.current_rank == "gold"
        assert state.highest_rank == "gold"
        assert state.current_stars == 1
        assert state.activation_state == "reassessment"
        assert state.source_observation_watermark == failed.sequence


def test_legacy_evidence_keeps_its_target_and_receives_audited_node_rank() -> None:
    with _session() as db:
        _add_target(db, target_id="target_legacy")
        _add_target(
            db,
            target_id="target_rank_destination",
            published_concept=True,
            rank_ceiling="bronze",
            bind_section=False,
        )
        db.add(
            AssessmentTargetRankIdentityDecision(
                id="rank_identity_decision_legacy",
                source_contract_version_id="contract_legacy",
                source_assessment_target_id="target_legacy",
                destination_assessment_target_id="target_rank_destination",
                decision="approved",
                basis_json="{}",
                rule_version="historical_rank_identity_v1",
                decision_hash="a" * 64,
                actor_kind="system_maintenance",
                actor_id="test",
            )
        )
        _add_observation(
            db,
            suffix="legacy",
            created_at=now(),
            correct=True,
            assistance_mode="unassisted_initial",
            target_id="target_legacy",
            learning_contract_version_id="contract_legacy",
        )
        db.flush()

        report = rebuild_assessment_projections(db, user_id="user_replay")
        target_state = db.scalar(select(KnowledgeStateProjection))
        node_state = db.scalar(select(KnowledgeNodeStateProjection))
        profile = db.scalar(select(LearnerKnowledgeProfileProjection))
        views = knowledge_node_views_for_targets(
            db,
            user_id="user_replay",
            target_ids={"target_legacy"},
            learning_contract_version_id="contract_legacy",
        )

        assert report["knowledgeStates"] == 1
        assert report["knowledgeNodeStates"] == 1
        assert target_state.assessment_target_id == "target_legacy"
        assert node_state.concept_revision_id == (
            "concept_revision_target_rank_destination"
        )
        assert node_state.current_rank == "bronze"
        assert next(iter(views.values()))["rank"] == "bronze"
        summary = json.loads(profile.summary_json)
        assert summary["nodeCount"] == 1
        assert summary["rankedNodeCount"] == 1


def test_legacy_target_without_audited_identity_cannot_settle() -> None:
    with _session() as db:
        _add_target(db, target_id="target_unmapped")
        db.flush()

        with pytest.raises(AppError) as error:
            require_effective_rank_targets(
                db,
                learning_contract_version_id="contract_unmapped",
                target_ids={"target_unmapped"},
            )

        assert error.value.code == "KNOWLEDGE_SETTLEMENT_IDENTITY_UNAVAILABLE"
