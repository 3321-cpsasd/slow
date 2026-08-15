import json

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.infrastructure.tables import (
    AssessmentObservation,
    AssessmentTarget,
    Base,
    CapabilityRevision,
    CapabilityStageCriterion,
    CapabilityStateProjection,
    Concept,
    ConceptRevision,
    EvidenceQualificationEvent,
    KnowledgeNodeStateProjection,
    SectionAssessmentTarget,
    now,
)
from app.modules.learning.assessment import (
    QUALIFICATION_RULE_VERSION,
    rebuild_assessment_projections,
)
from app.modules.learning.capabilities import ensure_route_capability


def _session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Session(engine)


def _add_concept(db: Session) -> ConceptRevision:
    db.add(
        Concept(
            id="concept_recursion",
            namespace="test",
            concept_key="recursion",
            canonical_name="递归",
            status="active",
            origin="test",
        )
    )
    revision = ConceptRevision(
        id="revision_recursion",
        concept_id="concept_recursion",
        revision=1,
        label="递归",
        definition="通过调用自身解决可递归分解的问题。",
        scope_json=json.dumps(
            {
                "rankPolicy": {
                    "version": "knowledge_rank_policy_v1",
                    "capabilityScope": "把递归迁移到陌生问题",
                    "rankCeiling": "diamond",
                    "dimensionRanks": {"transfer": "diamond"},
                }
            }
        ),
        verification_status="route_scoped",
    )
    db.add(revision)
    db.flush()
    return revision


def _add_target(
    db: Session,
    *,
    target_id: str,
    capability_revision_id: str,
    criterion_id: str,
    dimension: str = "transfer",
    position: int = 1,
) -> AssessmentTarget:
    target = AssessmentTarget(
        id=target_id,
        concept_revision_id="revision_recursion",
        capability_revision_id=capability_revision_id,
        capability_stage_criterion_id=criterion_id,
        objective_key=f"key_{target_id}",
        objective_statement="识别递归的核心含义",
        dimension=dimension,
        target_depth="standard",
        identity_status="route_scoped_knowledge",
        status="active",
    )
    db.add(target)
    db.add(
        SectionAssessmentTarget(
            id=f"binding_{target_id}",
            section_id="section_capability",
            assessment_target_id=target_id,
            position=position,
            required=True,
            verification_policy="choice_quiz_v1",
        )
    )
    return target


def _add_observation(
    db: Session,
    *,
    suffix: str,
    target_id: str,
    episode_id: str | None = None,
) -> AssessmentObservation:
    observed_at = now()
    observation = AssessmentObservation(
        id=f"observation_{suffix}",
        learning_run_id="run_capability",
        user_id="user_capability",
        section_id="section_capability",
        attempt_id=f"attempt_{suffix}",
        scoring_result_id=f"scoring_{suffix}",
        assessment_target_id=target_id,
        question_index=0,
        correct=True,
        source_type="choice_quiz",
        assistance_mode="unassisted_initial",
        learning_episode_id=episode_id or f"episode_{suffix}",
        equivalence_group_id=f"equivalence_{suffix}",
        qualification_at_creation="eligible",
        qualification_rule_version=QUALIFICATION_RULE_VERSION,
        payload_json="{}",
        created_at=observed_at,
    )
    db.add(observation)
    db.flush()
    for family, status in {
        "gate": "eligible",
        "mastery": "eligible_grouped",
        "retention": "ineligible",
        "rank": "eligible_grouped",
        "capability": "eligible_grouped",
    }.items():
        db.add(
            EvidenceQualificationEvent(
                id=f"qualification_{suffix}_{family}",
                observation_id=observation.id,
                projection_family=family,
                status=status,
                reason="test qualification",
                rule_version=QUALIFICATION_RULE_VERSION,
                created_at=observed_at,
            )
        )
    return observation


def test_choice_transfer_evidence_is_diamond_in_legacy_but_bronze_in_capability_profile() -> None:
    with _session() as db:
        _add_concept(db)
        capability, bronze = ensure_route_capability(
            db,
            series_id="series_capability",
            concept_revision_id="revision_recursion",
        )
        _add_target(
            db,
            target_id="target_transfer_choice",
            capability_revision_id=capability.id,
            criterion_id=bronze.id,
        )
        _add_observation(
            db,
            suffix="transfer_choice",
            target_id="target_transfer_choice",
        )
        db.flush()

        report = rebuild_assessment_projections(db, user_id="user_capability")
        old_state = db.scalar(select(KnowledgeNodeStateProjection))
        new_state = db.scalar(select(CapabilityStateProjection))

        assert old_state.current_rank == "diamond"
        assert new_state.current_stage == "bronze"
        assert new_state.highest_stage == "bronze"
        assert report["capabilityStates"] == 1
        assert report["qualifiedCapabilityObservations"] == 1


def test_cumulative_projection_cannot_skip_missing_bronze_criterion() -> None:
    with _session() as db:
        _add_concept(db)
        capability, bronze = ensure_route_capability(
            db,
            series_id="series_capability",
            concept_revision_id="revision_recursion",
        )
        capability.natural_stage_ceiling = "silver"
        silver = db.scalar(
            select(CapabilityStageCriterion).where(
                CapabilityStageCriterion.capability_revision_id == capability.id,
                CapabilityStageCriterion.stage == "silver",
            )
        )
        _add_target(
            db,
            target_id="target_silver",
            capability_revision_id=capability.id,
            criterion_id=silver.id,
            position=1,
        )
        _add_observation(db, suffix="silver_only", target_id="target_silver")
        db.flush()

        rebuild_assessment_projections(db, user_id="user_capability")
        state = db.scalar(select(CapabilityStateProjection))

        assert state.current_stage == "unranked"
        assert json.loads(state.satisfied_criterion_ids_json) == [silver.id]
        assert json.loads(state.missing_criterion_ids_json) == [bronze.id]

        _add_target(
            db,
            target_id="target_bronze",
            capability_revision_id=capability.id,
            criterion_id=bronze.id,
            dimension="recognition",
            position=2,
        )
        _add_observation(db, suffix="bronze_after", target_id="target_bronze")
        db.flush()

        rebuild_assessment_projections(db, user_id="user_capability")
        db.refresh(state)

        assert state.current_stage == "silver"
        assert state.current_stage_order == 2
        assert json.loads(state.missing_criterion_ids_json) == []
        assert state.independent_evidence_count == 2


def test_same_concept_reuses_capability_within_series_but_not_across_series() -> None:
    with _session() as db:
        _add_concept(db)
        first, first_bronze = ensure_route_capability(
            db,
            series_id="series_one",
            concept_revision_id="revision_recursion",
        )
        repeated, repeated_bronze = ensure_route_capability(
            db,
            series_id="series_one",
            concept_revision_id="revision_recursion",
        )
        other_series, _ = ensure_route_capability(
            db,
            series_id="series_two",
            concept_revision_id="revision_recursion",
        )

        assert repeated.id == first.id
        assert repeated_bronze.id == first_bronze.id
        assert other_series.id != first.id
