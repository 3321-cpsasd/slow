"""Immutable audit snapshots for server-owned learning decisions."""

from dataclasses import asdict
import hashlib
import json
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ...core.errors import AppError
from ...infrastructure.tables import (
    AssessmentGateState,
    AssessmentObservation,
    AssessmentTarget,
    LearningContractAssessmentTarget,
    LearningDecisionSnapshot,
    QuizAttempt,
    SectionAssessmentTarget,
)
from .assessment import GATE_RULE_VERSION, SectionGateDecision
from .domain import (
    PROGRESSION_RULE_VERSION,
    ProgressionDecision,
    ProgressionSnapshot,
)
from .knowledge_ranks import (
    KNOWLEDGE_RANK_RULE_VERSION,
    RANK_SETTLEABLE_IDENTITY_STATUSES,
    knowledge_node_views_for_targets,
    knowledge_settlement,
)


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def _dump(value) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _append(
    db: Session,
    *,
    attempt: QuizAttempt,
    section_id: str,
    decision_kind: str,
    trigger_kind: str,
    rule_version: str,
    input_snapshot: dict,
    output_decision: dict,
    source_observation_watermark: int,
) -> LearningDecisionSnapshot:
    idempotency_key = f"attempt:{attempt.id}:{decision_kind}:{rule_version}"
    existing = db.scalar(
        select(LearningDecisionSnapshot).where(
            LearningDecisionSnapshot.decision_kind == decision_kind,
            LearningDecisionSnapshot.idempotency_key == idempotency_key,
        )
    )
    if existing:
        return existing
    input_json = _dump(input_snapshot)
    row = LearningDecisionSnapshot(
        id=_uid("learning_decision"),
        learning_run_id=attempt.learning_run_id,
        user_id=attempt.user_id,
        section_id=section_id,
        attempt_id=attempt.id,
        learning_contract_version_id=attempt.learning_contract_version_id,
        content_version_id=attempt.content_version_id,
        decision_kind=decision_kind,
        trigger_kind=trigger_kind,
        rule_version=rule_version,
        input_snapshot_json=input_json,
        output_decision_json=_dump(output_decision),
        source_observation_watermark=source_observation_watermark,
        input_hash=hashlib.sha256(input_json.encode()).hexdigest(),
        idempotency_key=idempotency_key,
    )
    db.add(row)
    db.flush()
    return row


def append_assessment_gate_snapshot(
    db: Session,
    *,
    attempt: QuizAttempt,
    section_id: str,
    decision: SectionGateDecision,
    trigger_kind: str,
    decision_basis: str = "qualified_observation_gate",
    rule_version: str = GATE_RULE_VERSION,
) -> LearningDecisionSnapshot:
    """Freeze target requirements, projection inputs, score and gate output."""

    gate_states = db.scalars(
        select(AssessmentGateState)
        .where(
            AssessmentGateState.learning_run_id == attempt.learning_run_id,
            AssessmentGateState.section_id == section_id,
        )
        .order_by(AssessmentGateState.assessment_target_id)
    ).all()
    if attempt.learning_contract_version_id:
        required_target_ids = tuple(
            db.scalars(
                select(
                    LearningContractAssessmentTarget.assessment_target_id
                ).where(
                    LearningContractAssessmentTarget.contract_version_id
                    == attempt.learning_contract_version_id,
                    LearningContractAssessmentTarget.required.is_(True),
                )
            ).all()
        )
    else:
        required_target_ids = tuple(
            db.scalars(
                select(SectionAssessmentTarget.assessment_target_id).where(
                    SectionAssessmentTarget.section_id == section_id,
                    SectionAssessmentTarget.required.is_(True),
                )
            ).all()
        )
    observation_watermark = db.scalar(
        select(func.max(AssessmentObservation.sequence)).where(
            AssessmentObservation.learning_run_id == attempt.learning_run_id,
            AssessmentObservation.section_id == section_id,
        )
    ) or 0
    source_watermark = max([
        observation_watermark,
        *(state.source_observation_watermark for state in gate_states),
    ])
    input_snapshot = {
        "attemptId": attempt.id,
        "decisionBasis": decision_basis,
        "learningContractVersionId": attempt.learning_contract_version_id,
        "contentVersionId": attempt.content_version_id,
        "requiredTargetIds": sorted(required_target_ids),
        "gateStates": [
            {
                "assessmentTargetId": state.assessment_target_id,
                "status": state.status,
                "resolvedByObservationId": state.resolved_by_observation_id,
                "projectionRuleVersion": state.projection_rule_version,
                "projectionVersion": state.projection_version,
                "sourceObservationWatermark": state.source_observation_watermark,
            }
            for state in gate_states
        ],
        "score": {
            "initial": decision.initial_score,
            "adjusted": decision.adjusted_score,
            "fixedTotal": decision.fixed_total,
        },
    }
    output = {
        "passed": decision.passed,
        "unresolvedRequiredTargetIds": list(
            decision.unresolved_required_target_ids
        ),
        "unresolvedTargetIds": list(decision.unresolved_target_ids),
        "score": {
            "initial": decision.initial_score,
            "adjusted": decision.adjusted_score,
            "fixedTotal": decision.fixed_total,
        },
    }
    return _append(
        db,
        attempt=attempt,
        section_id=section_id,
        decision_kind="assessment_gate",
        trigger_kind=trigger_kind,
        rule_version=rule_version,
        input_snapshot=input_snapshot,
        output_decision=output,
        source_observation_watermark=source_watermark,
    )


def append_progression_snapshot(
    db: Session,
    *,
    attempt: QuizAttempt,
    section_id: str,
    snapshot: ProgressionSnapshot,
    decision: ProgressionDecision,
    trigger_kind: str,
) -> LearningDecisionSnapshot:
    """Freeze the exact pure-policy input and output before applying it."""

    observation_watermark = db.scalar(
        select(func.max(AssessmentObservation.sequence)).where(
            AssessmentObservation.learning_run_id == attempt.learning_run_id,
            AssessmentObservation.section_id == section_id,
        )
    ) or 0
    return _append(
        db,
        attempt=attempt,
        section_id=section_id,
        decision_kind="progression",
        trigger_kind=trigger_kind,
        rule_version=PROGRESSION_RULE_VERSION,
        input_snapshot=asdict(snapshot),
        output_decision=asdict(decision),
        source_observation_watermark=observation_watermark,
    )


def append_knowledge_settlement_snapshot(
    db: Session,
    *,
    attempt: QuizAttempt,
    section_id: str,
    target_ids: set[str],
    before: dict[str, dict],
    trigger_kind: str,
) -> dict:
    """Freeze the user-facing knowledge change derived from immutable facts."""

    after = knowledge_node_views_for_targets(
        db,
        user_id=attempt.user_id,
        target_ids=target_ids,
    )
    expected_concept_ids = set(
        db.scalars(
            select(AssessmentTarget.concept_revision_id).where(
                AssessmentTarget.id.in_(target_ids),
                AssessmentTarget.identity_status.in_(
                    RANK_SETTLEABLE_IDENTITY_STATUSES
                ),
                AssessmentTarget.concept_revision_id.is_not(None),
            )
        ).all()
    )
    missing_concept_ids = expected_concept_ids - set(after)
    if missing_concept_ids:
        raise AppError(
            "本次能力结算不完整，答案尚未写入，请稍后重试",
            code="KNOWLEDGE_SETTLEMENT_INCOMPLETE",
            status=500,
        )
    output = knowledge_settlement(before, after)
    source_watermark = max(
        (
            int(view.get("sourceObservationWatermark", 0))
            for view in [*before.values(), *after.values()]
        ),
        default=0,
    )
    snapshot = _append(
        db,
        attempt=attempt,
        section_id=section_id,
        decision_kind="knowledge_settlement",
        trigger_kind=trigger_kind,
        rule_version=KNOWLEDGE_RANK_RULE_VERSION,
        input_snapshot={
            "attemptId": attempt.id,
            "assessmentTargetIds": sorted(target_ids),
            "before": before,
            "after": after,
        },
        output_decision=output,
        source_observation_watermark=source_watermark,
    )
    # If another execution already froze this attempt/rule decision, always
    # return that immutable output instead of recomputing a different replay.
    frozen_output = json.loads(snapshot.output_decision_json)
    return {**frozen_output, "settlementId": snapshot.id}
