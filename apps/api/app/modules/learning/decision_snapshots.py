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
    LearningContractAssessmentTarget,
    LearningDecisionSnapshot,
    QuizAttempt,
    SectionAssessmentTarget,
)
from .assessment import GATE_RULE_VERSION, SectionGateDecision
from .capability_profiles import (
    CAPABILITY_PROJECTION_RULE_VERSION,
    capability_state_views_for_targets,
)
from .domain import (
    PROGRESSION_RULE_VERSION,
    ProgressionDecision,
    ProgressionSnapshot,
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


def append_capability_settlement_snapshot(
    db: Session,
    *,
    attempt: QuizAttempt,
    section_id: str,
    target_ids: set[str],
    before: dict[str, dict],
    trigger_kind: str,
) -> dict:
    """Freeze the four-stage capability change derived from immutable facts."""

    after = capability_state_views_for_targets(
        db, user_id=attempt.user_id, target_ids=target_ids
    )
    if target_ids and not after:
        raise AppError(
            "本次验证缺少稳定能力身份，答案尚未写入，请稍后重试",
            code="CAPABILITY_SETTLEMENT_IDENTITY_MISSING",
            status=500,
        )
    updates = []
    for capability_id, current in sorted(after.items()):
        previous = before.get(capability_id, {
            **current,
            "stage": "unranked",
            "stageOrder": 0,
            "stageLabel": "尚未验证",
            "highestStage": "unranked",
            "activationState": "learning",
            "stabilityDays": 0,
            "nextDueAt": None,
            "evidenceCount": 0,
            "independentEvidenceCount": 0,
            "satisfiedCriterionIds": [],
            "sourceObservationWatermark": 0,
        })
        if current["stageOrder"] > previous["stageOrder"]:
            change = "stage_up"
            message = f"你已经用正式任务证明自己达到{current['stageLabel']}。"
        elif (
            previous["activationState"] == "due_for_reactivation"
            and current["activationState"] == "available"
        ):
            change = "reactivated"
            message = "这项能力已经重新恢复为可调用状态。"
        elif current["activationState"] == "due_for_reactivation":
            change = "needs_reactivation"
            message = "历史阶段保留，但这项能力需要一次短唤醒。"
        elif current["evidenceCount"] > previous["evidenceCount"]:
            change = "evidence_added"
            message = "本次验证增加了独立证据，能力阶段按累计标准保持。"
        else:
            change = "confirmed"
            message = "本次结果与现有能力判断一致，没有重复制造晋级。"
        updates.append({
            "capabilityRevisionId": capability_id,
            "label": current["label"],
            "before": previous,
            "after": current,
            "change": change,
            "message": message,
        })
    output = {
        "schemaVersion": "capability_settlement_v1",
        "ruleVersion": CAPABILITY_PROJECTION_RULE_VERSION,
        "updates": updates,
    }
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
        decision_kind="capability_settlement",
        trigger_kind=trigger_kind,
        rule_version=CAPABILITY_PROJECTION_RULE_VERSION,
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
