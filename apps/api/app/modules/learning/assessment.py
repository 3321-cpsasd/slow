import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...core.errors import AppError
from ...infrastructure.tables import (
    AssessmentGateState,
    AssessmentObservation,
    AssessmentTarget,
    EvidenceQualificationEvent,
    GovernanceDecisionSnapshot,
    KnowledgeStateProjection,
    LearningContractAssessmentTarget,
    LearningContractVersion,
    ReviewState,
    ScoringResult,
    Section,
    SectionAssessmentTarget,
    now,
)
from .contracts import ensure_learning_contract


SCORING_RULE_VERSION = "choice_exact_v2"
QUALIFICATION_RULE_VERSION = "evidence_v2"
GATE_RULE_VERSION = "gate_v2"
BKT_PARAMETER_VERSION = "bkt_v1"
MASTERY_RULE_VERSION = "mastery_v2"
REVIEW_RULE_VERSION = "review_v2"
RETENTION_WINDOW = timedelta(days=1)
QUALIFIED_STATUSES_BY_FAMILY = {
    "gate": frozenset({"eligible"}),
    "mastery": frozenset({"eligible", "eligible_grouped"}),
    "retention": frozenset({"eligible", "candidate"}),
}


@dataclass(frozen=True)
class SectionGateDecision:
    passed: bool
    initial_score: int
    adjusted_score: int
    fixed_total: int
    unresolved_required_target_ids: tuple[str, ...]
    unresolved_target_ids: tuple[str, ...]


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def _dump(value) -> str:
    return json.dumps(value, ensure_ascii=False)


def _load(value: str, default):
    try:
        return json.loads(value) if value else default
    except json.JSONDecodeError:
        return default


def _normalized(value: str) -> str:
    return " ".join(value.strip().lower().split())


def _objective_key(statement: str) -> str:
    return hashlib.sha256(_normalized(statement).encode()).hexdigest()


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _contract_objectives(section: Section) -> list[tuple[str, bool]]:
    raw = _load(section.objectives_json, [])
    parsed: list[tuple[str, bool | None]] = []
    for item in raw:
        if isinstance(item, dict):
            statement = str(item.get("statement") or item.get("objective") or "").strip()
            required = item.get("required", item.get("core"))
        else:
            statement = str(item).strip()
            required = None
        if statement:
            parsed.append((statement, bool(required) if required is not None else None))
    if not parsed:
        parsed = [(section.question.strip(), True)]

    # Until section contracts carry an explicit required flag, the first declared
    # objective is the deterministic server-owned core. AI output never changes it.
    result: list[tuple[str, bool]] = []
    positions: dict[str, int] = {}
    for position, (statement, explicit_required) in enumerate(parsed):
        key = _objective_key(statement)
        required = explicit_required if explicit_required is not None else position == 0
        if key in positions:
            previous_statement, previous_required = result[positions[key]]
            result[positions[key]] = (previous_statement, previous_required or required)
        else:
            positions[key] = len(result)
            result.append((statement, required))
    return result


def ensure_section_targets(
    db: Session,
    section: Section,
) -> dict[str, AssessmentTarget]:
    """Materialize server-owned identities and contract-local gate attributes."""

    existing = {
        item.objective_key: item
        for item in db.scalars(select(AssessmentTarget)).all()
    }
    targets: dict[str, AssessmentTarget] = {}
    for position, (statement, required) in enumerate(_contract_objectives(section), 1):
        key = _objective_key(statement)
        target = existing.get(key)
        if not target:
            target = AssessmentTarget(
                id=_uid("target"),
                objective_key=key,
                objective_statement=statement,
                dimension="recognition",
                target_depth="standard",
                status="active",
            )
            db.add(target)
            db.flush()
            existing[key] = target
        binding = db.scalar(
            select(SectionAssessmentTarget).where(
                SectionAssessmentTarget.section_id == section.id,
                SectionAssessmentTarget.assessment_target_id == target.id,
            )
        )
        if not binding:
            binding = SectionAssessmentTarget(
                id=_uid("section_target"),
                section_id=section.id,
                assessment_target_id=target.id,
                position=position,
                required=required,
                verification_policy="choice_quiz_v1",
            )
            db.add(binding)
        else:
            binding.position = position
            binding.required = required
        targets[key] = target
    return targets


def assessment_contract_view(
    db: Session,
    section: Section,
    contract: LearningContractVersion | None = None,
) -> list[dict]:
    contract = contract or ensure_learning_contract(db, section)
    rows = db.execute(
        select(LearningContractAssessmentTarget, AssessmentTarget)
        .join(
            AssessmentTarget,
            AssessmentTarget.id
            == LearningContractAssessmentTarget.assessment_target_id,
        )
        .where(
            LearningContractAssessmentTarget.contract_version_id == contract.id
        )
        .order_by(LearningContractAssessmentTarget.position)
    ).all()
    return [
        {
            "assessmentTargetId": target.id,
            "objective": target.objective_statement,
            "dimension": target.dimension,
            "targetDepth": contract.target_depth,
            "assessmentLevel": target.target_depth,
            "required": binding.required,
        }
        for binding, target in rows
    ]


def bind_questions_to_targets(
    db: Session,
    section: Section,
    questions: list[dict],
    contract: LearningContractVersion | None = None,
) -> list[dict]:
    contract = contract or ensure_learning_contract(db, section)
    contract_rows = db.execute(
        select(LearningContractAssessmentTarget, AssessmentTarget)
        .join(
            AssessmentTarget,
            AssessmentTarget.id
            == LearningContractAssessmentTarget.assessment_target_id,
        )
        .where(
            LearningContractAssessmentTarget.contract_version_id == contract.id
        )
        .order_by(LearningContractAssessmentTarget.position)
    ).all()
    target_by_id = {target.id: target for _binding, target in contract_rows}
    normalized_targets = {
        _normalized(target.objective_statement): target
        for target in target_by_id.values()
    }
    bindings = {
        binding.assessment_target_id: binding
        for binding, _target in contract_rows
    }
    bound = []
    for index, question in enumerate(questions):
        supplied_target_id = str(question.get("assessmentTargetId", "")).strip()
        statement = str(question.get("objective", "")).strip()
        target = target_by_id.get(supplied_target_id) if supplied_target_id else None
        if target is None and statement:
            target = normalized_targets.get(_normalized(statement))
        if not target:
            raise AppError(
                "题目引用了学习契约之外的测量目标",
                code="ASSESSMENT_TARGET_UNBOUND",
                status=502,
            )
        if supplied_target_id and supplied_target_id != target.id:
            raise AppError(
                "题目的测量目标引用与学习契约不一致",
                code="ASSESSMENT_TARGET_MISMATCH",
                status=502,
            )
        binding = bindings[target.id]
        payload = dict(question)
        payload["objective"] = target.objective_statement
        payload["assessmentTargetId"] = target.id
        payload["core"] = binding.required
        payload["equivalenceGroupId"] = (
            payload.get("equivalenceGroupId")
            or f"{target.id}:contract:{binding.verification_policy}:slot:{index}"
        )
        bound.append(payload)
    return bound


def _posterior(prior: float, correct: bool, *, guess: float, slip: float) -> float:
    if correct:
        numerator = prior * (1 - slip)
        denominator = numerator + (1 - prior) * guess
    else:
        numerator = prior * slip
        denominator = numerator + (1 - prior) * (1 - guess)
    return numerator / denominator if denominator else prior


def _signatures(observations: list[AssessmentObservation]) -> set[str]:
    result = {
        f"equivalence:{item.equivalence_group_id}"
        for item in observations
        if item.equivalence_group_id
    }
    for item in observations:
        fingerprint = str(_load(item.payload_json, {}).get("questionFingerprint", ""))
        if fingerprint:
            result.add(f"question:{fingerprint}")
    return result


def _episode_passed(observations: list[AssessmentObservation]) -> bool:
    return bool(observations) and (
        sum(item.correct for item in observations) / len(observations) >= 0.8
    )


def _sync_gate(
    db: Session,
    *,
    key: tuple[str, str, str],
    observations: list[AssessmentObservation],
    existing: AssessmentGateState | None,
) -> AssessmentGateState:
    run_id, section_id, target_id = key
    attempt_groups: dict[str, list[AssessmentObservation]] = defaultdict(list)
    for observation in observations:
        attempt_groups[observation.attempt_id].append(observation)
    episodes = sorted(
        attempt_groups.values(),
        key=lambda items: (min(_utc(item.created_at) for item in items), min(item.sequence for item in items)),
    )
    resolution = next((items for items in episodes if _episode_passed(items)), None)
    latest = max(observations, key=lambda item: item.sequence)
    if not existing:
        existing = AssessmentGateState(
            id=_uid("gate_state"),
            learning_run_id=run_id,
            user_id=latest.user_id,
            section_id=section_id,
            assessment_target_id=target_id,
        )
        db.add(existing)
    else:
        existing.projection_version = (existing.projection_version or 0) + 1
    if resolution:
        resolving = max(resolution, key=lambda item: item.sequence)
        existing.status = (
            "resolved_remediation"
            if any(item.assistance_mode == "assisted_immediate" for item in resolution)
            else "resolved_initial"
        )
        existing.resolved_by_observation_id = resolving.id
    else:
        existing.status = "unresolved"
        existing.resolved_by_observation_id = None
    existing.projection_rule_version = GATE_RULE_VERSION
    existing.source_observation_watermark = max(item.sequence for item in observations)
    existing.updated_at = now()
    return existing


def _sync_knowledge_and_review(
    db: Session,
    *,
    target_id: str,
    observations: list[AssessmentObservation],
    retention_observation_ids: set[str],
    required: bool,
    state: KnowledgeStateProjection | None,
    review: ReviewState | None,
) -> tuple[KnowledgeStateProjection, ReviewState]:
    episode_groups: dict[str, list[AssessmentObservation]] = defaultdict(list)
    for observation in observations:
        episode_groups[observation.learning_episode_id].append(observation)
    episodes = sorted(
        episode_groups.values(),
        key=lambda items: (min(_utc(item.created_at) for item in items), min(item.sequence for item in items)),
    )

    prior = 0.2
    claim_status = "unobserved"
    retention_rounds = 0
    seen_signatures: set[str] = set()
    previous_episode_at: datetime | None = None
    last_episode: list[AssessmentObservation] | None = None
    last_episode_at: datetime | None = None
    for items in episodes:
        event_at = max(_utc(item.created_at) for item in items)
        correct = _episode_passed(items)
        assisted = any(item.assistance_mode == "assisted_immediate" for item in items)
        prior = _posterior(
            prior,
            correct,
            guess=0.5 if assisted else 0.25,
            slip=0.12,
        )
        unassisted_review = all(
            item.assistance_mode == "unassisted_review" for item in items
        )
        signatures = _signatures(items)
        independent_review = (
            correct
            and unassisted_review
            and all(item.id in retention_observation_ids for item in items)
            and previous_episode_at is not None
            and event_at - previous_episode_at >= RETENTION_WINDOW
            and not signatures.intersection(seen_signatures)
        )
        if independent_review:
            retention_rounds += 1
            claim_status = "retained" if retention_rounds >= 2 else "verified_delayed"
        elif correct and claim_status not in {"verified_delayed", "retained"}:
            claim_status = "verified_immediate"
        elif not correct:
            claim_status = (
                "contradicted"
                if required and claim_status != "unobserved"
                else "learning"
            )
        previous_episode_at = max(previous_episode_at, event_at) if previous_episode_at else event_at
        seen_signatures.update(signatures)
        last_episode = items
        last_episode_at = event_at

    latest = max(observations, key=lambda item: item.sequence)
    if not state:
        state = KnowledgeStateProjection(
            id=_uid("knowledge_state"),
            user_id=latest.user_id,
            assessment_target_id=target_id,
        )
        db.add(state)
    else:
        state.projection_version = (state.projection_version or 0) + 1
    state.p_known_ppm = round(max(0.0, min(1.0, prior)) * 1_000_000)
    state.uncertainty_ppm = round(4 * prior * (1 - prior) * 1_000_000)
    state.claim_status = claim_status
    state.retention_rounds = retention_rounds
    state.parameter_set_version = BKT_PARAMETER_VERSION
    state.projection_rule_version = MASTERY_RULE_VERSION
    state.source_observation_watermark = max(item.sequence for item in observations)
    state.rebuilt_at = now()
    state.updated_at = now()

    assert last_episode is not None and last_episode_at is not None
    correct = _episode_passed(last_episode)
    if not review:
        review = ReviewState(
            id=_uid("review_state"),
            user_id=latest.user_id,
            assessment_target_id=target_id,
        )
        db.add(review)
    else:
        review.projection_version = (review.projection_version or 0) + 1
    review.status = "scheduled" if correct else "remediation_due"
    spacing_days = [1, 3, 7, 14][min(retention_rounds, 3)]
    review.next_due_at = last_episode_at + (
        timedelta(days=spacing_days) if correct else timedelta()
    )
    review.priority = 100 if required and not correct else 70 if not correct else 40
    review.reason = (
        "core_gap"
        if required and not correct
        else "knowledge_gap"
        if not correct
        else "retention_follow_up"
        if all(item.assistance_mode == "unassisted_review" for item in last_episode)
        else "initial_learning"
    )
    review.spacing_stage = retention_rounds
    review.projection_rule_version = REVIEW_RULE_VERSION
    review.source_observation_watermark = max(item.sequence for item in observations)
    review.updated_at = now()
    return state, review


def rebuild_assessment_projections(
    db: Session,
    *,
    user_id: str,
    qualification_rule_version: str = QUALIFICATION_RULE_VERSION,
) -> dict:
    """Replay assessment facts in event-time order; safe for late and duplicate delivery."""

    observations = db.scalars(
        select(AssessmentObservation)
        .where(AssessmentObservation.user_id == user_id)
        .order_by(AssessmentObservation.created_at, AssessmentObservation.sequence)
    ).all()
    observation_ids = [item.id for item in observations]
    qualification_by_observation_family: dict[
        tuple[str, str], EvidenceQualificationEvent
    ] = {}
    if observation_ids:
        qualification_events = db.scalars(
            select(EvidenceQualificationEvent)
            .where(
                EvidenceQualificationEvent.observation_id.in_(observation_ids),
                EvidenceQualificationEvent.rule_version
                == qualification_rule_version,
            )
            .order_by(
                EvidenceQualificationEvent.created_at,
                EvidenceQualificationEvent.id,
            )
        ).all()
        # The current schema normally permits one event per observation, family,
        # and rule. Reducing in event-time order also keeps replay correct if a
        # later schema allows append-only requalification events.
        for event in qualification_events:
            qualification_by_observation_family[
                (event.observation_id, event.projection_family)
            ] = event

    def qualified(observation: AssessmentObservation, family: str) -> bool:
        event = qualification_by_observation_family.get((observation.id, family))
        return bool(
            event
            and event.status in QUALIFIED_STATUSES_BY_FAMILY.get(
                family,
                frozenset(),
            )
        )

    bindings = {
        (item.section_id, item.assessment_target_id): item
        for item in db.scalars(select(SectionAssessmentTarget)).all()
    }
    existing_gates = {
        (item.learning_run_id, item.section_id, item.assessment_target_id): item
        for item in db.scalars(
            select(AssessmentGateState).where(AssessmentGateState.user_id == user_id)
        ).all()
    }
    existing_states = {
        item.assessment_target_id: item
        for item in db.scalars(
            select(KnowledgeStateProjection).where(
                KnowledgeStateProjection.user_id == user_id
            )
        ).all()
    }
    existing_reviews = {
        item.assessment_target_id: item
        for item in db.scalars(
            select(ReviewState).where(ReviewState.user_id == user_id)
        ).all()
    }

    by_gate: dict[tuple[str, str, str], list[AssessmentObservation]] = defaultdict(list)
    by_target_mastery: dict[str, list[AssessmentObservation]] = defaultdict(list)
    retention_observation_ids: set[str] = set()
    for observation in observations:
        if qualified(observation, "gate"):
            by_gate[
                (
                    observation.learning_run_id,
                    observation.section_id,
                    observation.assessment_target_id,
                )
            ].append(observation)
        if qualified(observation, "mastery"):
            by_target_mastery[observation.assessment_target_id].append(observation)
        if qualified(observation, "retention"):
            retention_observation_ids.add(observation.id)

    for key, items in by_gate.items():
        _sync_gate(db, key=key, observations=items, existing=existing_gates.pop(key, None))
    for stale in existing_gates.values():
        db.delete(stale)

    for target_id, items in by_target_mastery.items():
        required = any(
            bindings.get((item.section_id, target_id))
            and bindings[(item.section_id, target_id)].required
            for item in items
        )
        _sync_knowledge_and_review(
            db,
            target_id=target_id,
            observations=items,
            retention_observation_ids=retention_observation_ids,
            required=required,
            state=existing_states.pop(target_id, None),
            review=existing_reviews.pop(target_id, None),
        )
    for stale in existing_states.values():
        db.delete(stale)
    for stale in existing_reviews.values():
        db.delete(stale)
    db.flush()
    return {
        "observations": len(observations),
        "qualifiedGateObservations": sum(len(items) for items in by_gate.values()),
        "qualifiedMasteryObservations": sum(
            len(items) for items in by_target_mastery.values()
        ),
        "qualifiedRetentionObservations": len(retention_observation_ids),
        "qualificationRuleVersion": qualification_rule_version,
        "gates": len(by_gate),
        "knowledgeStates": len(by_target_mastery),
        "reviewStates": len(by_target_mastery),
    }


def section_gate_decision(
    db: Session,
    *,
    learning_run_id: str,
    section_id: str,
) -> SectionGateDecision:
    observations = db.scalars(
        select(AssessmentObservation)
        .where(
            AssessmentObservation.learning_run_id == learning_run_id,
            AssessmentObservation.section_id == section_id,
            AssessmentObservation.assistance_mode == "unassisted_initial",
        )
        .order_by(AssessmentObservation.created_at, AssessmentObservation.sequence)
    ).all()
    if not observations:
        return SectionGateDecision(False, 0, 0, 0, (), ())
    first_attempt_id = observations[0].attempt_id
    baseline = [item for item in observations if item.attempt_id == first_attempt_id]
    gate_by_target = {
        item.assessment_target_id: item
        for item in db.scalars(
            select(AssessmentGateState).where(
                AssessmentGateState.learning_run_id == learning_run_id,
                AssessmentGateState.section_id == section_id,
            )
        ).all()
    }
    contract_version_id = baseline[0].learning_contract_version_id
    if contract_version_id:
        bindings = db.scalars(
            select(LearningContractAssessmentTarget).where(
                LearningContractAssessmentTarget.contract_version_id
                == contract_version_id
            )
        ).all()
    else:
        bindings = db.scalars(
            select(SectionAssessmentTarget).where(
                SectionAssessmentTarget.section_id == section_id
            )
        ).all()
    resolved = {
        target_id
        for target_id, gate in gate_by_target.items()
        if gate.status != "unresolved"
    }
    initial_score = sum(item.correct for item in baseline)
    adjusted_score = sum(
        item.correct or item.assessment_target_id in resolved for item in baseline
    )
    baseline_targets = {item.assessment_target_id for item in baseline}
    unresolved = tuple(sorted(baseline_targets - resolved))
    unresolved_required = tuple(sorted(
        item.assessment_target_id
        for item in bindings
        if item.required and item.assessment_target_id not in resolved
    ))
    total = len(baseline)
    return SectionGateDecision(
        passed=bool(total) and adjusted_score / total >= 0.8 and not unresolved_required,
        initial_score=initial_score,
        adjusted_score=adjusted_score,
        fixed_total=total,
        unresolved_required_target_ids=unresolved_required,
        unresolved_target_ids=unresolved,
    )


def failed_target_ids_for_attempt(db: Session, *, attempt_id: str) -> set[str]:
    rows = db.scalars(
        select(AssessmentObservation).where(
            AssessmentObservation.attempt_id == attempt_id
        )
    ).all()
    return {item.assessment_target_id for item in rows if not item.correct}


def due_review_queue(
    db: Session,
    *,
    user_id: str,
    daily_budget: int,
    as_of: datetime | None = None,
) -> dict:
    as_of = _utc(as_of or now())
    budget = max(0, min(daily_budget, 100))
    due_rows = db.execute(
        select(ReviewState, AssessmentTarget)
        .join(AssessmentTarget, AssessmentTarget.id == ReviewState.assessment_target_id)
        .where(
            ReviewState.user_id == user_id,
            ReviewState.next_due_at.is_not(None),
            ReviewState.next_due_at <= as_of,
        )
    ).all()
    ranked = sorted(
        due_rows,
        key=lambda row: (
            -(
                row[0].priority
                + min(max((as_of - _utc(row[0].next_due_at)).days, 0), 30)
            ),
            _utc(row[0].next_due_at),
            row[0].assessment_target_id,
        ),
    )
    rows = ranked[:budget]
    return {
        "asOf": as_of.isoformat(),
        "dailyBudget": budget,
        "selectedCount": len(rows),
        "items": [
            {
                "assessmentTargetId": target.id,
                "objective": target.objective_statement,
                "status": review.status,
                "dueAt": _utc(review.next_due_at).isoformat(),
                "priority": review.priority,
                "effectivePriority": review.priority + min(
                    max((as_of - _utc(review.next_due_at)).days, 0),
                    30,
                ),
                "reason": review.reason,
                "spacingStage": review.spacing_stage,
                "projectionRuleVersion": review.projection_rule_version,
                "sourceObservationWatermark": review.source_observation_watermark,
            }
            for review, target in rows
        ],
    }


def _record_qualification_events(
    db: Session,
    observation: AssessmentObservation,
    *,
    qualification_profile: str = "standard",
) -> None:
    governance = (
        db.scalar(
            select(GovernanceDecisionSnapshot)
            .where(
                GovernanceDecisionSnapshot.decision_scope
                == "quiz_publication",
                GovernanceDecisionSnapshot.quiz_set_id
                == observation.quiz_set_id,
            )
            .order_by(GovernanceDecisionSnapshot.created_at.desc())
        )
        if observation.quiz_set_id
        else None
    )
    m2_governance_missing = bool(
        observation.learning_contract_version_id and governance is None
    )
    statuses = (
        {
            "gate": ("ineligible", "delayed review cannot rewrite the section gate"),
            "mastery": ("eligible_grouped", "assignment-bound review updates mastery once"),
            "retention": ("candidate", "server-qualified delayed unassisted novel review"),
        }
        if qualification_profile == "review_assignment"
        else {
            # Compatibility keeps the reading/unlock loop usable, but an
            # unverified M2 publication must not become mastery or retention.
            "gate": ("eligible", "compatibility gate only; content governance is unresolved"),
            "mastery": ("ineligible", "quiz is not governance-qualified for mastery"),
            "retention": ("ineligible", "quiz is not governance-qualified for retention"),
        }
        if m2_governance_missing or (
            governance and not governance.assessment_eligible
        )
        else {
            "gate": ("eligible", "attempt target outcome is aggregated"),
            "mastery": ("eligible_grouped", "one BKT update per learning episode and target"),
            "retention": (
                ("candidate", "requires a delayed unassisted novel review")
                if observation.assistance_mode == "unassisted_review"
                else ("ineligible", "not an unassisted review")
            ),
        }
    )
    for family, (status, reason) in statuses.items():
        db.add(EvidenceQualificationEvent(
            id=_uid("qualification"),
            observation_id=observation.id,
            projection_family=family,
            status=status,
            reason=reason,
            rule_version=QUALIFICATION_RULE_VERSION,
        ))


def record_scoring_facts(
    db: Session,
    *,
    attempt,
    section: Section,
    questions: list[dict],
    results: list[dict],
    score: int,
    total: int,
    passed: bool,
    assistance_mode: str,
    learning_episode_id: str | None = None,
    qualification_profile: str = "standard",
) -> ScoringResult:
    existing = db.scalar(
        select(ScoringResult).where(ScoringResult.attempt_id == attempt.id)
    )
    if existing:
        return existing
    scoring = ScoringResult(
        id=_uid("scoring"),
        attempt_id=attempt.id,
        scoring_rule_version=SCORING_RULE_VERSION,
        score=score,
        total=total,
        passed=passed,
        results_json=_dump(results),
    )
    db.add(scoring)
    db.flush()
    if getattr(attempt, "learning_contract_version_id", None):
        binding_rows = db.scalars(
            select(LearningContractAssessmentTarget).where(
                LearningContractAssessmentTarget.contract_version_id
                == attempt.learning_contract_version_id
            )
        ).all()
    else:
        binding_rows = db.scalars(
            select(SectionAssessmentTarget).where(
                SectionAssessmentTarget.section_id == section.id
            )
        ).all()
    bindings = {item.assessment_target_id: item for item in binding_rows}
    episode_id = learning_episode_id or f"quiz:{attempt.id}"
    for index, (question, result) in enumerate(zip(questions, results, strict=True)):
        target_id = str(question.get("assessmentTargetId", ""))
        if target_id not in bindings:
            raise AppError(
                "题目缺少服务端测量目标绑定",
                code="ASSESSMENT_TARGET_MISSING",
                status=409,
            )
        fingerprint = hashlib.sha256(_dump({
            "prompt": question.get("prompt", ""),
            "options": question.get("options", []),
            "correct": question.get("correct", []),
        }).encode()).hexdigest()
        observation = AssessmentObservation(
            id=_uid("observation"),
            learning_run_id=attempt.learning_run_id,
            user_id=attempt.user_id,
            section_id=section.id,
            attempt_id=attempt.id,
            quiz_set_id=attempt.quiz_set_id,
            learning_contract_version_id=getattr(
                attempt, "learning_contract_version_id", None
            ),
            content_version_id=getattr(attempt, "content_version_id", None),
            scoring_result_id=scoring.id,
            assessment_target_id=target_id,
            question_index=index,
            correct=bool(result["correct"]),
            assistance_mode=assistance_mode,
            learning_episode_id=episode_id,
            equivalence_group_id=str(question.get("equivalenceGroupId", "")),
            qualification_at_creation="eligible_grouped",
            qualification_rule_version=QUALIFICATION_RULE_VERSION,
            payload_json=_dump({
                "selectedOptions": result.get("selectedOptions", []),
                "correctOptions": result.get("correctOptions", []),
                "questionFingerprint": fingerprint,
            }),
        )
        db.add(observation)
        db.flush()
        _record_qualification_events(
            db,
            observation,
            qualification_profile=qualification_profile,
        )
    rebuild_assessment_projections(db, user_id=attempt.user_id)
    return scoring
