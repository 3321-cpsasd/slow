"""Persistent delayed-review application service.

The assignment is the authority for review eligibility.  A free-standing quiz
or a repeated section quiz can never manufacture retention evidence.
"""

from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
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
    ContentVersion,
    LearningContractVersion,
    LearningMissionVersion,
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
from .content_governance_store import (
    bind_remediation_questions_to_source_claims,
    governance_view_for_quiz,
    reevaluate_generated_governance,
)
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


_REVIEW_BLOCK_ROLE_COMPATIBILITY = {
    "core_instruction": "conclusion",
    "prerequisite_scaffold": "transition",
    "comparison": "example",
    "application": "example",
    "transfer": "example",
    "summary": "conclusion",
}


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
            "role": _REVIEW_BLOCK_ROLE_COMPATIBILITY.get(role, role),
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


def _normalized(value: str) -> str:
    return re.sub(r"[\W_]+", "", value, flags=re.UNICODE).casefold()


def _question_signature(question: dict) -> str:
    return hashlib.sha256(_dump({
        "prompt": question.get("prompt", ""),
        "options": question.get("options", []),
        "correct": sorted(question.get("correct", [])),
    }).encode()).hexdigest()


def _questions_are_substantively_different(previous: dict, candidate: dict) -> bool:
    previous_prompt = _normalized(str(previous.get("prompt", "")))
    candidate_prompt = _normalized(str(candidate.get("prompt", "")))
    previous_options = frozenset(
        _normalized(str(item)) for item in previous.get("options", [])
    )
    candidate_options = frozenset(
        _normalized(str(item)) for item in candidate.get("options", [])
    )
    return bool(
        candidate_prompt
        and candidate_options
        and candidate_prompt != previous_prompt
        and candidate_options != previous_options
        and _question_signature(candidate) != _question_signature(previous)
    )


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
        for observation in observations:
            quiz = self.db.get(QuizSet, observation.quiz_set_id)
            if quiz and any(
                item.get("assessmentTargetId") == target_id
                for item in _load(quiz.questions_json, [])
            ):
                return observation
        raise AppError(
            "复习目标缺少可追溯的原始内容与题目",
            code="REVIEW_SOURCE_MISSING",
            status=409,
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
            candidates = [
                ReviewCandidate(
                    review_state_id=item.id,
                    assessment_target_id=item.assessment_target_id,
                    due_at=_utc(item.next_due_at),
                    priority=item.priority,
                    status=item.status,
                )
                for item in states
                if item.assessment_target_id not in active_target_ids
            ]
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
                source = self._source_for_target(item.assessment_target_id)
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
                }
                for item in assignments
            ],
        }

    def _question_for_target(self, quiz: QuizSet, target_id: str) -> dict:
        question = next((
            item for item in _load(quiz.questions_json, [])
            if item.get("assessmentTargetId") == target_id
        ), None)
        if not question:
            raise AppError("原题缺少测量目标绑定", code="REVIEW_PRIOR_QUESTION_MISSING", status=409)
        return question

    def _assignment_view(self, assignment: ReviewAssignment) -> dict:
        quiz = self.db.get(QuizSet, assignment.review_quiz_set_id) if assignment.review_quiz_set_id else None
        questions = _load(quiz.questions_json, []) if quiz else []
        return {
            "assignmentId": assignment.id,
            "status": assignment.status,
            "assessmentTargetId": assignment.assessment_target_id,
            "dueAt": _utc(assignment.due_at).isoformat(),
            "expiresAt": _utc(assignment.expires_at).isoformat(),
            "quiz": {
                "id": quiz.id,
                "questions": [
                    {
                        **{
                            key: value
                            for key, value in item.items()
                            if key not in {
                                "correct",
                                "explanation",
                                "claim_block_indexes",
                            }
                        },
                        "selectionMode": (
                            "multiple"
                            if len(set(item.get("correct", []))) > 1
                            else "single"
                        ),
                    }
                    for item in questions
                ],
            } if quiz else None,
            "attemptId": assignment.submitted_attempt_id,
        }

    async def start(self, assignment_id: str, *, as_of: datetime | None = None) -> dict:
        moment = _utc(as_of or now())
        assignment = self._owned(assignment_id)
        if assignment.status == "started":
            return self._assignment_view(assignment)
        if assignment.status != "presented":
            raise AppError("复习任务当前不可开始", code="REVIEW_ASSIGNMENT_TRANSITION_INVALID", status=409)
        prior_quiz = self.db.get(QuizSet, assignment.prior_quiz_set_id)
        content = self.db.get(ContentVersion, assignment.content_version_id)
        section = self.db.get(Section, assignment.source_section_id)
        target = self.db.get(AssessmentTarget, assignment.assessment_target_id)
        if not prior_quiz or not content or not section or not target:
            raise AppError("复习来源链不完整", code="REVIEW_SOURCE_MISSING", status=409)
        prior = self._question_for_target(prior_quiz, target.id)
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
        question = review_question.model_dump()
        question["assessmentTargetId"] = target.id
        question["equivalenceGroupId"] = f"{target.id}:review:{assignment.id}"
        if not _questions_are_substantively_different(prior, question):
            raise AppError("模型未生成实质不同的复习题", code="REVIEW_QUIZ_NOT_NOVEL", status=502)
        signature = _question_signature(question)
        prior_signatures = set(_load(assignment.prior_item_signatures_json, []))
        if signature in prior_signatures:
            raise AppError("模型复用了历史题目", code="REVIEW_QUIZ_NOT_NOVEL", status=502)
        question = bind_remediation_questions_to_source_claims(
            self.db,
            content=content,
            questions=[question],
            prior_questions=[prior],
        )[0]
        generation = self.db.scalar(
            select(func.max(QuizSet.generation)).where(QuizSet.section_id == section.id)
        ) or 0
        quiz = QuizSet(
            id=_uid("review_quiz"),
            section_id=section.id,
            content_version_id=content.id,
            learning_contract_version_id=assignment.learning_contract_version_id,
            generation=generation + 1,
            questions_json=_dump([question]),
        )
        self.db.add(quiz)
        self.db.flush()
        governance = reevaluate_generated_governance(
            self.db,
            quiz_id=quiz.id,
            actor_id=assignment.id,
            actor_kind="review_assignment",
        )
        if not (
            governance["allowed"] and governance["assessmentEligible"]
        ):
            raise AppError(
                "复习题缺少已核验的正文与主张绑定",
                code="REVIEW_QUIZ_GOVERNANCE_FAILED",
                status=409,
            )
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
        questions = _load(quiz.questions_json, []) if quiz else []
        if not quiz or not questions:
            raise AppError("复习题不存在", code="REVIEW_QUIZ_MISSING", status=409)
        governance = governance_view_for_quiz(self.db, quiz.id)
        if not governance or not (
            governance["allowed"] and governance["assessmentEligible"]
        ):
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
