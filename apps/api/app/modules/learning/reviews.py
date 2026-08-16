"""Persistent delayed-review application service.

The assignment is the authority for review eligibility.  A free-standing quiz
or a repeated section quiz can never manufacture retention evidence.
"""

from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
from typing import ClassVar
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ...ai.contracts import GeneratedContent, GeneratedQuiz
from ...core.errors import AppError
from ...domain.learning import grade_choice_quiz
from ...infrastructure.tables import (
    AssessmentObservation,
    AssessmentTarget,
    CapabilityRevision,
    CapabilityApplicationTaskVersion,
    CapabilityRouteBinding,
    CapabilityStageCriterion,
    CapabilityStateProjection,
    ContentVersion,
    LearningContractVersion,
    LearningMissionVersion,
    LearningRun,
    QuizAttempt,
    QuizSet,
    ReviewAssignment,
    ReviewAssignmentEventRecord,
    ReviewSelectionRun,
    ReviewState,
    Section,
    now,
)
from .assessment import record_scoring_facts
from .assessment_items import immutable_questions_for_quiz
from .derived_quizzes import (
    load_derived_quiz_source,
    publish_derived_quiz_candidate,
    public_question_view,
    question_signature as _question_signature,
    questions_are_substantively_different as _questions_are_substantively_different,
    with_alignment_gated_answer,
)
from .knowledge_ranks import (
    knowledge_node_views_for_targets,
    resolve_effective_rank_target,
)
from .content_governance_store import governance_view_for_quiz
from .review_assignments import (
    RETENTION_QUALIFICATION_RULE_VERSION,
    REVIEW_ASSIGNMENT_RULE_VERSION,
    ReviewAssignmentRuleError,
    ReviewAssignmentState,
    ReviewCandidate,
    ReviewSubmission,
    qualify_retention_submission,
    scheduled_assignment,
    select_daily_reviews,
    transition_assignment,
)
from .review_task_plans import (
    REVIEW_TASK_PLAN_RULE_VERSION,
    ReviewCriterion,
    plan_review_tasks,
)
from .review_stage_tasks import CapabilityReviewTaskService


logger = logging.getLogger(__name__)


class _ReviewGenerationContent(GeneratedContent):
    enforce_standard_sentence_endings: ClassVar[bool] = False


def _content_for_review_generation(content: ContentVersion) -> GeneratedContent:
    """Project published lesson blocks into the legacy quiz-generation view.

    ContentVersion stores the authoritative published v2 block payload.  The
    delayed-review quiz generator still consumes the smaller GeneratedContent
    contract, so only the fields it needs are copied across this boundary.
    """

    blocks = _load(content.blocks_json, [])
    projected_blocks = []
    for block in blocks:
        if not isinstance(block, dict):
            raise AppError(
                "复习所需的教材内容暂时不可用，请稍后重试",
                code="REVIEW_CONTENT_UNAVAILABLE",
                status=409,
                retryable=True,
            )
        role = str(block.get("role", ""))
        kind = str(block.get("kind", ""))
        projected_blocks.append({
            "id": block.get("id", ""),
            "version": block.get("version", 1),
            "kind": "text" if kind in {"bullet_list", "ordered_steps"} else kind,
            "role": role,
            "heading": block.get("heading", ""),
            "content": block.get("content", ""),
            "source_indexes": block.get("source_indexes", []),
            "assessment_objectives": block.get("assessment_objectives", []),
        })
    try:
        return _ReviewGenerationContent.model_validate({
            "confidence": content.confidence,
            "sources": _load(content.sources_json, []),
            "blocks": projected_blocks,
        })
    except ValueError as error:
        raise AppError(
            "复习所需的教材内容暂时不可用，请稍后重试",
            code="REVIEW_CONTENT_UNAVAILABLE",
            status=409,
            retryable=True,
        ) from error


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def _dump(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _load(value: str, default):
    try:
        return json.loads(value) if value else default
    except (TypeError, ValueError):
        return default


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


class ReviewAssignmentService:
    def __init__(
        self,
        db: Session,
        *,
        user_id: str,
        ai,
        context_builder=None,
        context_resolver=None,
        memory_loader=None,
    ):
        self.db = db
        self.user_id = user_id
        self.ai = ai
        self.context_builder = context_builder
        self.context_resolver = context_resolver
        self.memory_loader = memory_loader

    def _state(self, assignment: ReviewAssignment) -> ReviewAssignmentState:
        return ReviewAssignmentState(
            assignment_id=assignment.id,
            user_id=assignment.user_id,
            assessment_target_id=assignment.assessment_target_id,
            due_at=_utc(assignment.due_at),
            expires_at=_utc(assignment.expires_at),
            status=assignment.status,
            last_event_at=_utc(assignment.last_event_at),
            rule_version=assignment.selection_rule_version,
        )

    def _owned(self, assignment_id: str) -> ReviewAssignment:
        assignment = self.db.scalar(
            select(ReviewAssignment).where(
                ReviewAssignment.id == assignment_id,
                ReviewAssignment.user_id == self.user_id,
            )
        )
        if not assignment:
            raise AppError("复习任务不存在", code="REVIEW_ASSIGNMENT_NOT_FOUND", status=404)
        return assignment

    def _apply_transition(
        self,
        assignment: ReviewAssignment,
        event_type: str,
        occurred_at: datetime,
        *,
        payload: dict | None = None,
        idempotency_key: str = "",
    ) -> None:
        try:
            transition = transition_assignment(
                self._state(assignment),
                event_type=event_type,
                occurred_at=occurred_at,
            )
        except ReviewAssignmentRuleError as error:
            raise AppError(str(error), code=error.code, status=409) from error
        assignment.status = transition.state.status
        assignment.last_event_at = transition.state.last_event_at
        assignment.updated_at = occurred_at
        self.db.add(ReviewAssignmentEventRecord(
            id=_uid("review_event"),
            assignment_id=assignment.id,
            event_type=event_type,
            occurred_at=occurred_at,
            rule_version=transition.event.rule_version,
            idempotency_key=idempotency_key,
            payload_json=_dump(payload or {}),
        ))

    def _source_for_target(self, target_id: str) -> AssessmentObservation:
        observations = self.db.scalars(
            select(AssessmentObservation)
            .where(
                AssessmentObservation.user_id == self.user_id,
                AssessmentObservation.assessment_target_id == target_id,
                AssessmentObservation.quiz_set_id.is_not(None),
                AssessmentObservation.learning_contract_version_id.is_not(None),
                AssessmentObservation.content_version_id.is_not(None),
            )
            .order_by(AssessmentObservation.sequence.desc())
        ).all()
        rejection_codes: dict[str, int] = {}
        for observation in observations:
            quiz = self.db.get(QuizSet, observation.quiz_set_id)
            if not quiz or observation.question_index is None:
                continue
            try:
                load_derived_quiz_source(
                    self.db,
                    quiz=quiz,
                    assessment_target_id=target_id,
                    question_position=observation.question_index,
                    expected_section_id=observation.section_id,
                    expected_content_version_id=observation.content_version_id,
                    expected_contract_version_id=(
                        observation.learning_contract_version_id
                    ),
                )
                if rejection_codes:
                    logger.info(
                        "review source selection used an older compatible "
                        "observation user_id=%s assessment_target_id=%s "
                        "rejection_codes=%s",
                        self.user_id,
                        target_id,
                        rejection_codes,
                        extra={
                            "review_user_id": self.user_id,
                            "assessment_target_id": target_id,
                            "rejection_codes": rejection_codes,
                        },
                    )
                return observation
            except AppError as error:
                rejection_codes[error.code] = rejection_codes.get(error.code, 0) + 1
                continue
        logger.info(
            "review source selection skipped incompatible observations "
            "user_id=%s assessment_target_id=%s rejection_codes=%s",
            self.user_id,
            target_id,
            rejection_codes,
            extra={
                "review_user_id": self.user_id,
                "assessment_target_id": target_id,
                "rejection_codes": rejection_codes,
            },
        )
        raise AppError(
            "复习目标缺少可追溯的原始内容与题目",
            code="REVIEW_SOURCE_MISSING",
            status=409,
            details={"rejectionCodes": rejection_codes},
        )

    def _prior_signatures(self, target_id: str) -> set[str]:
        rows = self.db.scalars(
            select(AssessmentObservation).where(
                AssessmentObservation.user_id == self.user_id,
                AssessmentObservation.assessment_target_id == target_id,
            )
        ).all()
        result = set()
        for row in rows:
            fingerprint = str(_load(row.payload_json, {}).get("questionFingerprint", ""))
            if fingerprint:
                result.add(fingerprint)
        return result

    def _task_plan(
        self,
        *,
        target: AssessmentTarget,
        capability_state: CapabilityStateProjection | None,
        source: AssessmentObservation,
        remediation_due: bool,
    ) -> dict:
        if not target.capability_revision_id or capability_state is None:
            return {
                "ruleVersion": REVIEW_TASK_PLAN_RULE_VERSION,
                "reactivation": {
                    "purpose": "retention_reactivation",
                    "taskKind": "choice_reactivation",
                    "stage": "bronze",
                    "criterionIds": [],
                    "verificationProtocols": ["choice_quiz_v1"],
                    "evidenceEffect": "activation_only",
                },
                "strengthening": None,
            }
        run = self.db.get(LearningRun, source.learning_run_id)
        route = self.db.scalar(
            select(CapabilityRouteBinding).where(
                CapabilityRouteBinding.series_id == run.series_id,
                CapabilityRouteBinding.capability_revision_id
                == target.capability_revision_id,
                CapabilityRouteBinding.status == "active",
            )
        ) if run else None
        if route is None:
            raise AppError(
                "能力复习缺少当前系列的正式任务路线",
                code="REVIEW_CAPABILITY_ROUTE_MISSING",
                status=409,
            )
        criteria = self.db.scalars(
            select(CapabilityStageCriterion)
            .where(
                CapabilityStageCriterion.capability_revision_id
                == target.capability_revision_id,
                CapabilityStageCriterion.required.is_(True),
            )
            .order_by(CapabilityStageCriterion.position)
        ).all()
        available = frozenset(
            str(item.get("criterionId"))
            for item in _load(route.opportunities_json, [])
            if item.get("criterionId")
        )
        try:
            return plan_review_tasks(
                current_stage=capability_state.current_stage,
                criteria=tuple(
                    ReviewCriterion(
                        criterion_id=item.id,
                        stage=item.stage,
                        position=item.position,
                        task_type=item.task_type,
                        verification_protocol=item.verification_protocol,
                    )
                    for item in criteria
                ),
                missing_criterion_ids=tuple(
                    _load(capability_state.missing_criterion_ids_json, [])
                ),
                available_criterion_ids=available,
                remediation_due=remediation_due,
            )
        except ValueError as error:
            raise AppError(
                "当前能力阶段没有可执行的正式复习任务",
                code="REVIEW_CAPABILITY_TASK_PLAN_INVALID",
                status=409,
            ) from error

    def due(self, *, daily_budget: int = 10, as_of: datetime | None = None) -> dict:
        moment = _utc(as_of or now())
        budget = max(0, min(daily_budget, 100))
        selection_date = moment.date().isoformat()
        selection_run = self.db.scalar(
            select(ReviewSelectionRun).where(
                ReviewSelectionRun.user_id == self.user_id,
                ReviewSelectionRun.selection_date == selection_date,
                ReviewSelectionRun.rule_version == REVIEW_ASSIGNMENT_RULE_VERSION,
            )
        )
        if not selection_run:
            active_assignments = self.db.scalars(
                select(ReviewAssignment).where(
                    ReviewAssignment.user_id == self.user_id,
                    ReviewAssignment.status.in_({"scheduled", "presented", "started"}),
                )
            ).all()
            active_target_ids: set[str] = set()
            for assignment in active_assignments:
                if _utc(assignment.expires_at) < moment:
                    self._apply_transition(assignment, "expired", moment)
                else:
                    active_target_ids.add(assignment.assessment_target_id)
            states = self.db.scalars(
                select(ReviewState).where(
                    ReviewState.user_id == self.user_id,
                    ReviewState.next_due_at.is_not(None),
                )
            ).all()
            target_rows = {
                item.id: item
                for item in self.db.scalars(
                    select(AssessmentTarget).where(
                        AssessmentTarget.id.in_(
                            [item.assessment_target_id for item in states]
                        )
                    )
                ).all()
            } if states else {}
            capability_ids = {
                item.capability_revision_id
                for item in target_rows.values()
                if item.capability_revision_id
            }
            capability_states = {
                item.capability_revision_id: item
                for item in self.db.scalars(
                    select(CapabilityStateProjection).where(
                        CapabilityStateProjection.user_id == self.user_id,
                        CapabilityStateProjection.capability_revision_id.in_(
                            capability_ids
                        ),
                    )
                ).all()
            } if capability_ids else {}
            source_by_target: dict[str, AssessmentObservation] = {}
            task_plan_by_target: dict[str, dict] = {}
            candidates = []
            for item in states:
                if item.assessment_target_id in active_target_ids:
                    continue
                try:
                    source = self._source_for_target(item.assessment_target_id)
                except AppError as error:
                    if error.code != "REVIEW_SOURCE_MISSING":
                        raise
                    continue
                source_by_target[item.assessment_target_id] = source
                target = target_rows.get(item.assessment_target_id)
                capability = (
                    capability_states.get(target.capability_revision_id)
                    if target and target.capability_revision_id
                    else None
                )
                capability_activation = (
                    "due_for_reactivation"
                    if capability
                    and capability.next_due_at is not None
                    and _utc(capability.next_due_at) <= moment
                    else capability.activation_state
                    if capability
                    else ""
                )
                try:
                    task_plan_by_target[item.assessment_target_id] = self._task_plan(
                        target=target,
                        capability_state=capability,
                        source=source,
                        remediation_due=item.status == "remediation_due",
                    )
                except AppError as error:
                    if error.code not in {
                        "REVIEW_CAPABILITY_ROUTE_MISSING",
                        "REVIEW_CAPABILITY_TASK_PLAN_INVALID",
                    }:
                        raise
                    logger.info(
                        "review selection skipped capability without executable task "
                        "user_id=%s assessment_target_id=%s code=%s",
                        self.user_id,
                        item.assessment_target_id,
                        error.code,
                    )
                    continue
                candidates.append(ReviewCandidate(
                    review_state_id=item.id,
                    assessment_target_id=item.assessment_target_id,
                    due_at=_utc(item.next_due_at),
                    priority=item.priority,
                    status=item.status,
                    need_kind=(
                        "remediation"
                        if item.status == "remediation_due"
                        else "activation_due"
                    ),
                    capability_stage=(
                        capability.current_stage if capability else "unranked"
                    ),
                    capability_activation_state=(
                        capability_activation
                    ),
                    capability_revision_id=(
                        target.capability_revision_id
                        if target and target.capability_revision_id
                        else ""
                    ),
                ))
            try:
                selection = select_daily_reviews(
                    candidates,
                    daily_budget=budget,
                    as_of=moment,
                )
            except ReviewAssignmentRuleError as error:
                raise AppError(str(error), code=error.code, status=409) from error
            input_hash = hashlib.sha256(_dump([
                {
                    "reviewStateId": item.review_state_id,
                    "targetId": item.assessment_target_id,
                    "dueAt": _utc(item.due_at).isoformat(),
                    "priority": item.priority,
                    "status": item.status,
                    "needKind": item.need_kind,
                    "capabilityStage": item.capability_stage,
                    "capabilityActivationState": (
                        item.capability_activation_state
                    ),
                    "capabilityRevisionId": item.capability_revision_id,
                }
                for item in sorted(candidates, key=lambda value: value.assessment_target_id)
            ]).encode()).hexdigest()
            selection_run = ReviewSelectionRun(
                id=_uid("review_selection"),
                user_id=self.user_id,
                selection_date=selection_date,
                as_of=moment,
                daily_budget=budget,
                due_count=selection.due_count,
                rule_version=selection.rule_version,
                input_hash=input_hash,
            )
            self.db.add(selection_run)
            self.db.flush()
            for item in selection.items:
                source = source_by_target[item.assessment_target_id]
                scheduled = scheduled_assignment(
                    assignment_id=_uid("review_assignment"),
                    user_id=self.user_id,
                    assessment_target_id=item.assessment_target_id,
                    due_at=item.due_at,
                    expires_at=max(item.due_at + timedelta(days=7), moment + timedelta(days=1)),
                    scheduled_at=moment,
                )
                self.db.add(ReviewAssignment(
                    id=scheduled.assignment_id,
                    selection_run_id=selection_run.id,
                    review_state_id=item.review_state_id,
                    user_id=self.user_id,
                    assessment_target_id=item.assessment_target_id,
                    source_learning_run_id=source.learning_run_id,
                    source_section_id=source.section_id,
                    learning_contract_version_id=source.learning_contract_version_id,
                    content_version_id=source.content_version_id,
                    prior_quiz_set_id=source.quiz_set_id,
                    due_at=scheduled.due_at,
                    expires_at=scheduled.expires_at,
                    status=scheduled.status,
                    rank=item.rank,
                    base_priority=item.base_priority,
                    effective_priority=item.effective_priority,
                    selection_rule_version=item.rule_version,
                    qualification_rule_version=RETENTION_QUALIFICATION_RULE_VERSION,
                    task_plan_json=_dump(
                        task_plan_by_target[item.assessment_target_id]
                    ),
                    task_plan_rule_version=REVIEW_TASK_PLAN_RULE_VERSION,
                    prior_item_signatures_json=_dump(sorted(self._prior_signatures(item.assessment_target_id))),
                    item_signatures_json="[]",
                    last_event_at=scheduled.last_event_at,
                ))
            self.db.flush()

        assignments = self.db.scalars(
            select(ReviewAssignment)
            .where(
                ReviewAssignment.selection_run_id == selection_run.id,
                ReviewAssignment.user_id == self.user_id,
            )
            .order_by(ReviewAssignment.rank, ReviewAssignment.id)
        ).all()
        for assignment in assignments:
            if assignment.status == "scheduled":
                self._apply_transition(assignment, "presented", moment)
        self.db.commit()
        return self._selection_view(selection_run, assignments)

    def _selection_view(self, selection: ReviewSelectionRun, assignments: list[ReviewAssignment]) -> dict:
        targets = {
            item.id: item
            for item in self.db.scalars(
                select(AssessmentTarget).where(
                    AssessmentTarget.id.in_([item.assessment_target_id for item in assignments])
                )
            ).all()
        } if assignments else {}
        review_states = {
            item.id: item
            for item in self.db.scalars(
                select(ReviewState).where(
                    ReviewState.id.in_([item.review_state_id for item in assignments])
                )
            ).all()
        } if assignments else {}
        capability_ids = {
            target.capability_revision_id
            for target in targets.values()
            if target.capability_revision_id
        }
        capability_states = {
            item.capability_revision_id: item
            for item in self.db.scalars(
                select(CapabilityStateProjection).where(
                    CapabilityStateProjection.user_id == self.user_id,
                    CapabilityStateProjection.capability_revision_id.in_(
                        capability_ids
                    ),
                )
            ).all()
        } if capability_ids else {}
        capability_revisions = {
            item.id: item
            for item in self.db.scalars(
                select(CapabilityRevision).where(
                    CapabilityRevision.id.in_(capability_ids)
                )
            ).all()
        } if capability_ids else {}
        targets_by_contract: dict[str, set[str]] = {}
        for assignment in assignments:
            targets_by_contract.setdefault(
                assignment.learning_contract_version_id, set()
            ).add(assignment.assessment_target_id)
        node_views: dict[str, dict] = {}
        for contract_id, target_ids in targets_by_contract.items():
            node_views.update(
                knowledge_node_views_for_targets(
                    self.db,
                    user_id=self.user_id,
                    target_ids=target_ids,
                    learning_contract_version_id=contract_id,
                )
            )
        effective_concepts_by_assignment = {}
        for assignment in assignments:
            effective = resolve_effective_rank_target(
                self.db,
                source_target=targets[assignment.assessment_target_id],
                learning_contract_version_id=(
                    assignment.learning_contract_version_id
                ),
            )
            effective_concepts_by_assignment[assignment.id] = (
                effective.concept_revision_id if effective else None
            )

        def capability_view(assignment: ReviewAssignment) -> dict | None:
            capability_id = targets[
                assignment.assessment_target_id
            ].capability_revision_id
            state = capability_states.get(capability_id or "")
            revision = capability_revisions.get(capability_id or "")
            if state is None or revision is None:
                return None
            activation = (
                "due_for_reactivation"
                if state.next_due_at is not None
                and _utc(state.next_due_at) <= _utc(selection.as_of)
                else state.activation_state
            )
            return {
                "label": revision.label,
                "currentStage": state.current_stage,
                "activationState": activation,
            }

        return {
            "selectionRunId": selection.id,
            "asOf": _utc(selection.as_of).isoformat(),
            "dailyBudget": selection.daily_budget,
            "dueCount": selection.due_count,
            "selectedCount": len(assignments),
            "ruleVersion": selection.rule_version,
            "items": [
                {
                    "assignmentId": item.id,
                    "assessmentTargetId": item.assessment_target_id,
                    "objective": targets[item.assessment_target_id].objective_statement,
                    "status": item.status,
                    "dueAt": _utc(item.due_at).isoformat(),
                    "expiresAt": _utc(item.expires_at).isoformat(),
                    "rank": item.rank,
                    "basePriority": item.base_priority,
                    "effectivePriority": item.effective_priority,
                    "quizSetId": item.review_quiz_set_id,
                    "knowledgeNode": node_views.get(
                        effective_concepts_by_assignment[item.id] or ""
                    ),
                    "reviewReason": (
                        "近期作答暴露了需要补强的部分"
                        if review_states[item.review_state_id].status
                        == "remediation_due"
                        else "这项能力已经到复习时间"
                    ),
                    "capability": capability_view(item),
                    "taskPlan": _load(item.task_plan_json, {}),
                }
                for item in assignments
            ],
        }

    def _question_for_target(
        self,
        quiz: QuizSet,
        target_id: str,
        *,
        question_position: int,
    ) -> dict:
        questions = immutable_questions_for_quiz(
            self.db,
            quiz,
            require_versions=True,
            require_evidence=True,
            require_answer_versions=True,
        )
        if (
            question_position < 0
            or question_position >= len(questions)
            or questions[question_position].get("assessmentTargetId") != target_id
        ):
            raise AppError("原题缺少测量目标绑定", code="REVIEW_PRIOR_QUESTION_MISSING", status=409)
        return questions[question_position]

    def _source_for_assignment(self, assignment: ReviewAssignment):
        observation = self.db.scalar(
            select(AssessmentObservation)
            .where(
                AssessmentObservation.user_id == self.user_id,
                AssessmentObservation.learning_run_id
                == assignment.source_learning_run_id,
                AssessmentObservation.section_id == assignment.source_section_id,
                AssessmentObservation.quiz_set_id == assignment.prior_quiz_set_id,
                AssessmentObservation.content_version_id
                == assignment.content_version_id,
                AssessmentObservation.learning_contract_version_id
                == assignment.learning_contract_version_id,
                AssessmentObservation.assessment_target_id
                == assignment.assessment_target_id,
                AssessmentObservation.question_index.is_not(None),
            )
            .order_by(AssessmentObservation.sequence.desc())
        )
        if not observation or observation.question_index is None:
            raise AppError(
                "复习任务缺少精确的原始题目观察",
                code="REVIEW_SOURCE_MISSING",
                status=409,
            )
        quiz = self.db.get(QuizSet, assignment.prior_quiz_set_id)
        if not quiz:
            raise AppError("复习来源题集不存在", code="REVIEW_SOURCE_MISSING", status=409)
        return load_derived_quiz_source(
            self.db,
            quiz=quiz,
            assessment_target_id=assignment.assessment_target_id,
            question_position=observation.question_index,
            expected_section_id=assignment.source_section_id,
            expected_content_version_id=assignment.content_version_id,
            expected_contract_version_id=assignment.learning_contract_version_id,
        )

    def _assignment_view(self, assignment: ReviewAssignment) -> dict:
        quiz = self.db.get(QuizSet, assignment.review_quiz_set_id) if assignment.review_quiz_set_id else None
        questions = (
            immutable_questions_for_quiz(
                self.db,
                quiz,
                require_versions=True,
                require_evidence=True,
                require_answer_versions=True,
            )
            if quiz else []
        )
        capability_task = CapabilityReviewTaskService(
            self.db, user_id=self.user_id, ai=self.ai
        ).view_for_assignment(assignment.id)
        return {
            "assignmentId": assignment.id,
            "status": assignment.status,
            "assessmentTargetId": assignment.assessment_target_id,
            "dueAt": _utc(assignment.due_at).isoformat(),
            "expiresAt": _utc(assignment.expires_at).isoformat(),
            "quiz": {
                "id": quiz.id,
                "questions": [
                    public_question_view(item)
                    for item in questions
                ],
            } if quiz else None,
            "attemptId": assignment.submitted_attempt_id,
            "taskPlan": _load(assignment.task_plan_json, {}),
            "capabilityTask": capability_task,
        }

    def strengthening(self, assignment_id: str) -> dict:
        """Resolve the frozen next-stage action after successful reactivation.

        Reactivation evidence never advances the stage.  This endpoint only
        exposes the separately published formal opportunity frozen by the
        assignment plan; it does not create or silently substitute a task.
        """

        assignment = self._owned(assignment_id)
        if assignment.status != "submitted":
            raise AppError(
                "请先完成本次能力再激活",
                code="REVIEW_STRENGTHENING_REACTIVATION_REQUIRED",
                status=409,
            )
        result = _load(assignment.response_json, {})
        reactivated = bool(
            result.get("reactivationQualified")
            if "reactivationQualified" in result
            else result.get("passed")
        )
        if not reactivated:
            raise AppError(
                "当前能力尚未重新确认，请先完成针对性补强",
                code="REVIEW_STRENGTHENING_REACTIVATION_NOT_QUALIFIED",
                status=409,
            )
        plan = _load(assignment.task_plan_json, {})
        strengthening = plan.get("strengthening")
        if not isinstance(strengthening, dict):
            return {
                "schemaVersion": "capability_strengthening_launch_v1",
                "assignmentId": assignment.id,
                "status": "unavailable",
                "reason": "no_published_next_stage_opportunity",
                "entry": None,
            }
        target = self.db.get(AssessmentTarget, assignment.assessment_target_id)
        run = self.db.get(LearningRun, assignment.source_learning_run_id)
        capability_id = target.capability_revision_id if target else None
        if target is None or run is None or not capability_id:
            raise AppError(
                "下一阶强化缺少稳定能力路线",
                code="REVIEW_STRENGTHENING_ROUTE_MISSING",
                status=409,
            )
        state = self.db.scalar(
            select(CapabilityStateProjection).where(
                CapabilityStateProjection.user_id == self.user_id,
                CapabilityStateProjection.capability_revision_id == capability_id,
            )
        )
        stage_order = {"unranked": 0, "bronze": 1, "silver": 2, "gold": 3, "diamond": 4}
        target_stage = str(strengthening.get("stage") or "")
        if state and stage_order.get(state.current_stage, 0) >= stage_order.get(target_stage, 99):
            return {
                "schemaVersion": "capability_strengthening_launch_v1",
                "assignmentId": assignment.id,
                "status": "already_achieved",
                "stage": target_stage,
                "currentStage": state.current_stage,
                "entry": None,
            }
        route = self.db.scalar(
            select(CapabilityRouteBinding).where(
                CapabilityRouteBinding.series_id == run.series_id,
                CapabilityRouteBinding.capability_revision_id == capability_id,
                CapabilityRouteBinding.status == "active",
            )
        )
        if route is None:
            raise AppError(
                "下一阶强化路线已经失效",
                code="REVIEW_STRENGTHENING_ROUTE_MISSING",
                status=409,
            )
        criterion_ids = {
            str(item) for item in strengthening.get("criterionIds", []) if item
        }
        opportunities = [
            item
            for item in _load(route.opportunities_json, [])
            if str(item.get("criterionId") or "") in criterion_ids
        ]
        if {str(item.get("criterionId") or "") for item in opportunities} != criterion_ids:
            raise AppError(
                "下一阶正式验证机会已经变更，请等待新的复习安排",
                code="REVIEW_STRENGTHENING_OPPORTUNITY_STALE",
                status=409,
            )
        task_kind = str(strengthening.get("taskKind") or "")
        base = {
            "schemaVersion": "capability_strengthening_launch_v1",
            "assignmentId": assignment.id,
            "status": "ready",
            "stage": target_stage,
            "currentStage": state.current_stage if state else "unranked",
            "taskKind": task_kind,
            "criterionIds": sorted(criterion_ids),
            "evidenceEffect": "may_advance_stage_after_qualified_evidence",
        }
        if task_kind == "oral_strengthening":
            return {
                **base,
                "entry": {
                    "kind": "ask_me",
                    "seriesId": run.series_id,
                    "sectionId": assignment.source_section_id,
                    "label": "进入口试，讲清机制与边界",
                },
            }
        expected_kind = {
            "application_strengthening": "standard_application",
            "transfer_strengthening": "transfer_task",
        }.get(task_kind)
        if expected_kind is None or len(opportunities) != 1:
            raise AppError(
                "下一阶强化任务类型不可执行",
                code="REVIEW_STRENGTHENING_TASK_KIND_INVALID",
                status=409,
            )
        task_id = str(opportunities[0].get("taskVersionId") or "")
        task = self.db.get(CapabilityApplicationTaskVersion, task_id)
        if (
            task is None
            or task.task_kind != expected_kind
            or task.capability_revision_id != capability_id
            or task.capability_stage_criterion_id not in criterion_ids
            or task.publication_status not in {"published", "published_demo"}
        ):
            raise AppError(
                "下一阶正式任务已经失效",
                code="REVIEW_STRENGTHENING_TASK_STALE",
                status=409,
            )
        return {
            **base,
            "entry": {
                "kind": expected_kind,
                "seriesId": run.series_id,
                "sectionId": task.section_id,
                "task": {
                    "id": task.id,
                    "taskKind": task.task_kind,
                    "prompt": task.prompt,
                    "taskContext": _load(task.task_context_json, {}),
                    "deliverables": _load(task.deliverables_json, []),
                    "evidenceEligible": task.publication_status == "published",
                    "isDemo": task.provenance_mode == "local_demo",
                },
            },
        }

    async def start(self, assignment_id: str, *, as_of: datetime | None = None) -> dict:
        moment = _utc(as_of or now())
        assignment = self._owned(assignment_id)
        if assignment.status == "started":
            return self._assignment_view(assignment)
        if assignment.status != "presented":
            raise AppError("复习任务当前不可开始", code="REVIEW_ASSIGNMENT_TRANSITION_INVALID", status=409)
        task_plan = _load(assignment.task_plan_json, {})
        reactivation = task_plan.get("reactivation", {})
        task_kind = (
            reactivation.get("taskKind", "")
            if isinstance(reactivation, dict)
            else ""
        )
        if task_kind != "choice_reactivation":
            task = await CapabilityReviewTaskService(
                self.db,
                user_id=self.user_id,
                ai=self.ai,
            ).prepare(assignment)
            self._apply_transition(
                assignment,
                "started",
                moment,
                payload={"capabilityReviewTaskId": task.id, "taskKind": task.task_kind},
            )
            self.db.commit()
            return self._assignment_view(assignment)
        source = self._source_for_assignment(assignment)
        content = source.content
        section = source.section
        target = source.target
        prior = source.question
        generated_content = _content_for_review_generation(content)
        request = {
                "id": section.id,
                "title": section.title,
                "question": section.question,
                "objectives": [target.objective_statement],
                "reviewMode": "delayed_assignment",
                "reviewAssignmentId": assignment.id,
                "assessmentTargetId": target.id,
            }
        if self.context_builder and self.context_resolver and self.memory_loader:
            section_context = self.context_resolver.resolve_section(
                user_id=self.user_id,
                section_id=section.id,
            )
            contract = self.db.get(
                LearningContractVersion,
                assignment.learning_contract_version_id,
            )
            mission = (
                self.db.get(LearningMissionVersion, contract.mission_version_id)
                if contract
                else None
            )
            context_pack = self.context_builder.build(
                "review_quiz",
                shelf=section_context.shelf,
                series=section_context.series,
                book=section_context.book,
                chapter=section_context.chapter,
                section=section,
                mission=mission,
                contract=contract,
                memory=self.memory_loader(section_context.book.shelf_id, 30),
                interaction={
                    "reviewAssignmentId": assignment.id,
                    "priorQuestion": prior,
                },
            )
            request = self.context_builder.attach(request, context_pack)
        result = await self.ai.lesson_quiz(
            request,
            generated_content,
            [prior],
        )
        if len(result.questions) != 1:
            raise AppError("复习题数量与目标不一致", code="REVIEW_QUIZ_TARGET_MISMATCH", status=502)
        review_question = result.questions[0]
        review_question.objective = prior["objective"]
        review_question.core = bool(prior.get("core", False))
        alignment_reviewer = getattr(self.ai, "review_lesson_alignment", None)
        if not callable(alignment_reviewer):
            raise AppError(
                "当前 AI 不支持复习题语义对齐审查",
                code="REVIEW_ALIGNMENT_UNAVAILABLE",
                status=503,
                retryable=True,
            )
        alignment = await alignment_reviewer(
            request,
            generated_content,
            GeneratedQuiz(questions=[review_question]),
        )
        if not alignment.allowed:
            raise AppError(
                "复习题、正确答案与原正文未形成可验证的语义闭环",
                code="REVIEW_SEMANTIC_ALIGNMENT_FAILED",
                status=502,
                retryable=True,
            )
        question = with_alignment_gated_answer(review_question.model_dump())
        question["assessmentTargetId"] = target.id
        question["equivalenceGroupId"] = f"{target.id}:review:{assignment.id}"
        if not _questions_are_substantively_different(prior, question):
            raise AppError("模型未生成实质不同的复习题", code="REVIEW_QUIZ_NOT_NOVEL", status=502)
        signature = _question_signature(question)
        prior_signatures = set(_load(assignment.prior_item_signatures_json, []))
        if signature in prior_signatures:
            raise AppError("模型复用了历史题目", code="REVIEW_QUIZ_NOT_NOVEL", status=502)
        locked_assignment = self.db.scalar(
            select(ReviewAssignment)
            .where(
                ReviewAssignment.id == assignment.id,
                ReviewAssignment.user_id == self.user_id,
            )
            .with_for_update()
        )
        if not locked_assignment:
            raise AppError("复习任务不存在", code="REVIEW_ASSIGNMENT_NOT_FOUND", status=404)
        if locked_assignment.status == "started":
            return self._assignment_view(locked_assignment)
        if locked_assignment.status != "presented":
            raise AppError("复习任务当前不可开始", code="REVIEW_ASSIGNMENT_TRANSITION_INVALID", status=409)
        assignment = locked_assignment
        generation = self.db.scalar(
            select(func.max(QuizSet.generation)).where(QuizSet.section_id == section.id)
        ) or 0
        publication = publish_derived_quiz_candidate(
            self.db,
            uid=_uid,
            source=source,
            candidate_question=question,
            kind="review",
            quiz_generation=generation + 1,
            actor_kind="review_assignment",
            actor_id=assignment.id,
            equivalence_group_id=f"{target.id}:review:{assignment.id}",
        )
        quiz = publication.quiz
        question = publication.question
        assignment.review_quiz_set_id = quiz.id
        assignment.item_signatures_json = _dump([signature])
        self._apply_transition(
            assignment,
            "started",
            moment,
            payload={"quizSetId": quiz.id, "itemSignatures": [signature]},
        )
        self.db.commit()
        return self._assignment_view(assignment)

    async def respond(
        self,
        assignment_id: str,
        *,
        response: dict,
        assistance_used: bool,
        idempotency_key: str | None,
        as_of: datetime | None = None,
    ) -> dict:
        moment = _utc(as_of or now())
        assignment = self._owned(assignment_id)
        if assignment.status not in {"started", "submitted"}:
            raise AppError(
                "能力复习任务尚未开始",
                code="CAPABILITY_REVIEW_TASK_NOT_STARTED",
                status=409,
            )
        plan = _load(assignment.task_plan_json, {})
        reactivation = plan.get("reactivation", {})
        if not isinstance(reactivation, dict) or reactivation.get(
            "taskKind"
        ) == "choice_reactivation":
            raise AppError(
                "选择题复习必须通过答案提交入口完成",
                code="CAPABILITY_REVIEW_ENDPOINT_KIND_MISMATCH",
                status=409,
            )
        result, _submission = await CapabilityReviewTaskService(
            self.db,
            user_id=self.user_id,
            ai=self.ai,
        ).submit(
            assignment,
            response=response,
            assistance_used=assistance_used,
            idempotency_key=idempotency_key or "",
        )
        if assignment.status == "started":
            self._apply_transition(
                assignment,
                "submitted",
                moment,
                payload={
                    "submissionId": result["submissionId"],
                    "reactivationQualified": result["reactivationQualified"],
                    "evidenceEffect": "activation_only",
                },
                idempotency_key=idempotency_key or "",
            )
            assignment.response_json = _dump(result)
        self.db.commit()
        return result

    def submit(
        self,
        assignment_id: str,
        answers: list[list[int]],
        *,
        idempotency_key: str | None = None,
        as_of: datetime | None = None,
    ) -> dict:
        moment = _utc(as_of or now())
        assignment = self._owned(assignment_id)
        if assignment.status == "submitted" and assignment.response_json:
            attempt = self.db.get(QuizAttempt, assignment.submitted_attempt_id)
            replay_key = (idempotency_key or f"review:{assignment.id}").strip()
            replay_hash = hashlib.sha256(_dump({
                "assignmentId": assignment.id,
                "quizSetId": assignment.review_quiz_set_id,
                "answers": answers,
            }).encode()).hexdigest()
            if (
                not attempt
                or attempt.idempotency_key != replay_key
                or attempt.request_hash != replay_hash
            ):
                raise AppError(
                    "答题请求标识已用于不同内容",
                    code="IDEMPOTENCY_KEY_REUSED",
                    status=409,
                )
            return _load(assignment.response_json, {})
        if assignment.status != "started" or not assignment.review_quiz_set_id:
            raise AppError("复习任务尚未开始", code="REVIEW_ASSIGNMENT_NOT_STARTED", status=409)
        quiz = self.db.get(QuizSet, assignment.review_quiz_set_id)
        questions = (
            immutable_questions_for_quiz(
                self.db,
                quiz,
                require_versions=True,
                require_evidence=True,
                require_answer_versions=True,
            )
            if quiz else []
        )
        if (
            not quiz
            or quiz.publication_status != "published"
            or not questions
        ):
            raise AppError("复习题不存在", code="REVIEW_QUIZ_MISSING", status=409)
        governance = governance_view_for_quiz(self.db, quiz.id)
        if not governance or not (
            governance["allowed"] and governance["assessmentEligible"]
        ) or governance["mode"] != "contract_boundary":
            raise AppError(
                "复习题的可信治理决策缺失或已失效",
                code="REVIEW_QUIZ_GOVERNANCE_REQUIRED",
                status=409,
            )
        prior_signatures = frozenset(_load(assignment.prior_item_signatures_json, []))
        item_signatures = frozenset(_load(assignment.item_signatures_json, []))
        qualification = qualify_retention_submission(
            self._state(assignment),
            ReviewSubmission(
                assignment_id=assignment.id,
                assessment_target_id=assignment.assessment_target_id,
                submitted_at=moment,
                assistance_mode="unassisted_review",
                item_signatures=item_signatures,
                prior_item_signatures=prior_signatures,
            ),
        )
        if not qualification.eligible:
            raise AppError(
                "复习提交不满足保持证据条件：" + ", ".join(qualification.reasons),
                code="REVIEW_RETENTION_INELIGIBLE",
                status=409,
            )
        try:
            grade = grade_choice_quiz(questions, answers)
        except (KeyError, TypeError, ValueError) as error:
            raise AppError("复习答案格式无效", code="REVIEW_ANSWERS_INVALID", status=400) from error
        request_key = (idempotency_key or f"review:{assignment.id}").strip()
        if not 8 <= len(request_key) <= 128:
            raise AppError("答题请求标识无效", code="IDEMPOTENCY_KEY_INVALID", status=400)
        request_hash = hashlib.sha256(_dump({
            "assignmentId": assignment.id,
            "quizSetId": quiz.id,
            "answers": answers,
        }).encode()).hexdigest()
        attempt = QuizAttempt(
            id=_uid("review_attempt"),
            quiz_set_id=quiz.id,
            learning_contract_version_id=assignment.learning_contract_version_id,
            content_version_id=assignment.content_version_id,
            learning_run_id=assignment.source_learning_run_id,
            user_id=self.user_id,
            idempotency_key=request_key,
            request_hash=request_hash,
            answers_json=_dump(answers),
            results_json=_dump(grade.results),
            passed=grade.passed,
            workflow_status="succeeded",
        )
        self.db.add(attempt)
        self.db.flush()
        section = self.db.get(Section, assignment.source_section_id)
        record_scoring_facts(
            self.db,
            attempt=attempt,
            section=section,
            questions=questions,
            results=grade.results,
            score=grade.score,
            total=grade.total,
            passed=grade.passed,
            assistance_mode="unassisted_review",
            learning_episode_id=f"review:{assignment.id}",
            qualification_profile="review_assignment",
        )
        self._apply_transition(
            assignment,
            "submitted",
            moment,
            payload={
                "attemptId": attempt.id,
                "qualification": qualification.status,
                "qualificationRuleVersion": qualification.rule_version,
            },
            idempotency_key=request_key,
        )
        assignment.submitted_attempt_id = attempt.id
        response = {
            "assignmentId": assignment.id,
            "status": "submitted",
            "attemptId": attempt.id,
            "score": grade.score,
            "total": grade.total,
            "passed": grade.passed,
            "results": grade.results,
            "retentionQualification": {
                "status": "candidate",
                "ruleVersion": qualification.rule_version,
                "reasons": [],
            },
            "reinforcement": {
                "available": not grade.passed,
                "reason": "wake_failed" if not grade.passed else "not_needed",
            },
        }
        assignment.response_json = _dump(response)
        self.db.commit()
        return response

    def skip(self, assignment_id: str, *, as_of: datetime | None = None) -> dict:
        moment = _utc(as_of or now())
        assignment = self._owned(assignment_id)
        if assignment.status == "skipped":
            return self._assignment_view(assignment)
        self._apply_transition(assignment, "skipped", moment)
        self.db.commit()
        return self._assignment_view(assignment)

    def expire(self, assignment_id: str, *, as_of: datetime | None = None) -> dict:
        moment = _utc(as_of or now())
        assignment = self._owned(assignment_id)
        if assignment.status == "expired":
            return self._assignment_view(assignment)
        self._apply_transition(assignment, "expired", moment)
        self.db.commit()
        return self._assignment_view(assignment)
