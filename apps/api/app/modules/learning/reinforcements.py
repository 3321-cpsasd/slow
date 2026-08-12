"""Bounded, evidence-guided reinforcement after a failed delayed review.

The service owns the state machine. AI supplies one candidate verification item;
diagnosis and teaching activities never become mastery, rank, retention, or gate
evidence. Only the final novel, governed, unassisted verification is recorded.
"""

import hashlib
import json
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ...ai.contracts import GeneratedQuiz
from ...core.errors import AiError, AppError, safe_error_code
from ...domain.learning import grade_choice_quiz
from ...infrastructure.tables import (
    AssessmentObservation,
    AssessmentTarget,
    ContentBlockVersion,
    ContentVersion,
    GenerationRun,
    QuizAttempt,
    QuizSet,
    ReinforcementActivityVersion,
    ReinforcementEventRecord,
    ReinforcementPackageVersion,
    ReinforcementRun,
    ReviewAssignment,
    Section,
    now,
)
from .assessment import record_scoring_facts
from .assessment_items import (
    immutable_questions_for_quiz,
    publish_assessment_item_versions,
)
from .content_governance_store import (
    bind_remediation_questions_to_source_claims,
    governance_view_for_quiz,
    reevaluate_generated_governance,
)
from .reviews import (
    _content_for_review_generation,
    _dump,
    _load,
    _question_signature,
    _questions_are_substantively_different,
)
from .remediation_diagnosis import (
    INSUFFICIENT_EVIDENCE,
    diagnose_failed_attempt,
)


REINFORCEMENT_STATE_RULE_VERSION = "reinforcement_state_v1"
REINFORCEMENT_SCHEMA_VERSION = "reinforcement_package_v1"
REINFORCEMENT_PROMPT_VERSION = "reinforcement_verify_v1"

CAUSE_OPTIONS = (
    ("prerequisite_gap", "前置概念没接上"),
    ("concept_confusion", "两个相近概念混在了一起"),
    ("mechanism_reasoning_break", "知道结论，但因果步骤断了"),
    ("boundary_comparison_error", "忽略了条件或适用边界"),
    ("application_transfer_failure", "换了情境后不会调用"),
)
CAUSE_LABELS = dict(CAUSE_OPTIONS)


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def _hash(value) -> str:
    return hashlib.sha256(_dump(value).encode()).hexdigest()


def _public_question(question: dict) -> dict:
    return {
        **{
            key: value
            for key, value in question.items()
            if key not in {
                "correct", "explanation", "claim_block_indexes",
                "distractor_diagnostics", "distractorDiagnostics",
            }
        },
        "selectionMode": "multiple" if len(set(question.get("correct", []))) > 1 else "single",
    }


class ReinforcementService:
    def __init__(self, db: Session, *, user_id: str, ai):
        self.db = db
        self.user_id = user_id
        self.ai = ai

    def _owned_run(self, run_id: str) -> ReinforcementRun:
        run = self.db.scalar(select(ReinforcementRun).where(
            ReinforcementRun.id == run_id,
            ReinforcementRun.user_id == self.user_id,
        ))
        if not run:
            raise AppError("补强任务不存在", code="REINFORCEMENT_NOT_FOUND", status=404)
        return run

    def _owned_failed_assignment(self, assignment_id: str) -> ReviewAssignment:
        assignment = self.db.scalar(select(ReviewAssignment).where(
            ReviewAssignment.id == assignment_id,
            ReviewAssignment.user_id == self.user_id,
        ))
        if not assignment:
            raise AppError("复习任务不存在", code="REVIEW_ASSIGNMENT_NOT_FOUND", status=404)
        if assignment.status != "submitted" or not assignment.submitted_attempt_id:
            raise AppError("请先完成本次唤醒", code="REINFORCEMENT_REVIEW_NOT_SUBMITTED", status=409)
        attempt = self.db.get(QuizAttempt, assignment.submitted_attempt_id)
        if not attempt or attempt.passed:
            raise AppError("这项能力已成功唤醒，不需要进入补强", code="REINFORCEMENT_NOT_REQUIRED", status=409)
        return assignment

    def _package(self, run_id: str) -> ReinforcementPackageVersion | None:
        return self.db.scalar(
            select(ReinforcementPackageVersion)
            .where(
                ReinforcementPackageVersion.run_id == run_id,
                ReinforcementPackageVersion.status == "published",
            )
            .order_by(ReinforcementPackageVersion.version.desc())
        )

    def _activities(self, package_id: str) -> dict[str, ReinforcementActivityVersion]:
        return {
            item.activity_key: item
            for item in self.db.scalars(
                select(ReinforcementActivityVersion)
                .where(ReinforcementActivityVersion.package_version_id == package_id)
                .order_by(ReinforcementActivityVersion.position)
            ).all()
        }

    def _activity_view(
        self,
        run: ReinforcementRun,
        activity: ReinforcementActivityVersion,
    ) -> dict:
        payload = _load(activity.payload_json, {})
        if activity.activity_type in {"recompose", "verify"}:
            payload = {**payload, "question": _public_question(payload["question"])}
        if activity.activity_type == "repair":
            cases_by_cause = payload.pop("casesByCause", {})
            selected_case = cases_by_cause.get(run.confirmed_cause_code) or next(
                iter(cases_by_cause.values()),
                None,
            )
            payload = {
                **payload,
                "case": selected_case,
                "round": max(1, run.repair_rounds),
                "heading": (
                    "换一条更短的线索再试一次"
                    if run.repair_rounds >= 2
                    else payload.get("heading", "把断点接回来")
                ),
            }
        return {
            "activityKey": activity.activity_key,
            "type": activity.activity_type,
            "evidenceRole": activity.evidence_role,
            "payload": payload,
        }

    def view(self, run_id: str, *, feedback: dict | None = None) -> dict:
        run = self._owned_run(run_id)
        target = self.db.get(AssessmentTarget, run.assessment_target_id)
        package = self._package(run.id)
        activity = None
        if package and run.status == "active":
            activity = self._activities(package.id).get(run.current_activity_key)
        stage_index = {
            "diagnose": 1, "repair": 2, "recompose": 3,
            "verify": 4, "complete": 5, "replan_required": 5,
        }.get(run.current_state, 0)
        return {
            "runId": run.id,
            "status": run.status,
            "state": run.current_state,
            "objective": target.objective_statement if target else "当前能力",
            "entryMode": run.entry_mode,
            "progress": {
                "stage": stage_index,
                "totalStages": 5,
                "activityCount": run.activity_count,
                "maxActivities": run.max_activities,
                "repairRounds": run.repair_rounds,
                "maxRepairRounds": run.max_repair_rounds,
            },
            "evidenceBoundary": (
                "诊断、提示和重组练习只用于找回理解；只有最后一道独立验证题可能更新掌握与段位，"
                "本次不会被记作延迟保持证据。"
            ),
            "currentActivity": self._activity_view(run, activity) if activity else None,
            "feedback": feedback,
            "outcome": (
                {"kind": "recovered", "message": "这项能力已经重新接通，后续仍会按遗忘节奏再次唤醒。"}
                if run.status == "completed"
                else {"kind": "needsReplan", "message": "连续验证仍未接通，系统不会继续刷题；下一步需要回到更小的前置能力。"}
                if run.status == "replan_required"
                else None
            ),
        }

    def active(self) -> dict | None:
        run = self.db.scalar(
            select(ReinforcementRun)
            .where(
                ReinforcementRun.user_id == self.user_id,
                ReinforcementRun.status.in_({"preparing", "active"}),
            )
            .order_by(ReinforcementRun.updated_at.desc(), ReinforcementRun.id.desc())
        )
        return self.view(run.id) if run else None

    async def start_for_review(self, assignment_id: str) -> dict:
        assignment = self._owned_failed_assignment(assignment_id)
        failed_attempt = self.db.get(QuizAttempt, assignment.submitted_attempt_id)
        if not failed_attempt:
            raise AppError("复习作答证据不存在", code="REINFORCEMENT_SOURCE_MISSING", status=409)
        abstained_diagnosis = {
            "causeCode": INSUFFICIENT_EVIDENCE,
            "status": "abstained",
            "confidence": 0.0,
            "evidenceCount": 0,
        }
        try:
            diagnosis = next(
                (
                    item for item in diagnose_failed_attempt(self.db, failed_attempt)
                    if item["assessmentTargetId"] == assignment.assessment_target_id
                ),
                abstained_diagnosis,
            )
        except AppError as error:
            if error.code not in {
                "ASSESSMENT_ITEM_VERSION_MISSING",
                "ASSESSMENT_ITEM_EVIDENCE_INCOMPLETE",
            }:
                raise
            # Legacy review quizzes remain usable, but they cannot manufacture a
            # diagnostic hypothesis without immutable item and evidence rows.
            diagnosis = abstained_diagnosis
        existing = self.db.scalar(select(ReinforcementRun).where(
            ReinforcementRun.source_review_assignment_id == assignment.id,
            ReinforcementRun.user_id == self.user_id,
        ))
        if existing and self._package(existing.id):
            return self.view(existing.id)

        if existing:
            existing.status = "preparing"
            existing.current_state = "prepare"
            existing.current_activity_key = "diagnose"
            existing.updated_at = now()

        run = existing or ReinforcementRun(
            id=_uid("reinforcement"),
            user_id=self.user_id,
            assessment_target_id=assignment.assessment_target_id,
            source_review_assignment_id=assignment.id,
            source_learning_run_id=assignment.source_learning_run_id,
            source_section_id=assignment.source_section_id,
            learning_contract_version_id=assignment.learning_contract_version_id,
            content_version_id=assignment.content_version_id,
            entry_mode="wake_failure",
            status="preparing",
            current_state="prepare",
            current_activity_key="diagnose",
            activity_count=0,
            repair_rounds=0,
            max_activities=5,
            max_repair_rounds=2,
            state_rule_version=REINFORCEMENT_STATE_RULE_VERSION,
        )
        run.confirmed_cause_code = str(diagnosis["causeCode"])
        self.db.add(run)
        self.db.flush()
        generation_attempt = (
            self.db.scalar(select(func.max(GenerationRun.attempt)).where(
                GenerationRun.section_id == assignment.source_section_id,
                GenerationRun.operation == "reinforcement",
            )) or 0
        ) + 1
        generation = GenerationRun(
            id=_uid("generation"),
            section_id=assignment.source_section_id,
            operation="reinforcement",
            attempt=generation_attempt,
            status="running",
            model=getattr(self.ai, "model", ""),
            pipeline_version=REINFORCEMENT_STATE_RULE_VERSION,
            prompt_version=REINFORCEMENT_PROMPT_VERSION,
            schema_version=REINFORCEMENT_SCHEMA_VERSION,
            generation_mode="model_only",
            context_hash="",
            trace_json="{}",
        )
        self.db.add(generation)
        self.db.commit()

        try:
            target = self.db.get(AssessmentTarget, assignment.assessment_target_id)
            section = self.db.get(Section, assignment.source_section_id)
            content = self.db.get(ContentVersion, assignment.content_version_id)
            failed_quiz = self.db.get(QuizSet, assignment.review_quiz_set_id)
            if not target or not section or not content or not failed_quiz:
                raise AppError("补强来源链不完整", code="REINFORCEMENT_SOURCE_MISSING", status=409)
            failed_questions = _load(failed_quiz.questions_json, [])
            failed_question = next((item for item in failed_questions if item.get("assessmentTargetId") == target.id), None)
            if not failed_question:
                raise AppError("补强目标缺少原始题目", code="REINFORCEMENT_SOURCE_MISSING", status=409)
            failed_results = _load(failed_attempt.results_json, [])
            failed_result = next(
                (
                    item for item in failed_results
                    if item.get("assessmentTargetId") == target.id
                ),
                failed_results[0] if failed_results else {},
            )
            selected_indexes = failed_result.get("selectedOptions", [])
            selected_labels = [
                failed_question.get("options", [])[index]
                for index in selected_indexes
                if isinstance(index, int)
                and 0 <= index < len(failed_question.get("options", []))
            ]
            diagnosis_supported = diagnosis["causeCode"] != INSUFFICIENT_EVIDENCE
            diagnosis_label = (
                CAUSE_LABELS.get(str(diagnosis["causeCode"]), "当前错误路径")
                if diagnosis_supported
                else "证据还不够，先不判断具体原因"
            )
            diagnosis_message = (
                f"你刚才选择了“{'、'.join(selected_labels)}”。这条作答路径更接近“{diagnosis_label}”，"
                "但目前只是待验证假设，不会写进你的学习画像。"
                if diagnosis_supported and selected_labels
                else "当前作答还没有形成一致、可解释的错误路径。Agent 会从最小关键连接开始，"
                "不先给你贴上薄弱类型。"
            )

            request = {
                "id": section.id,
                "title": section.title,
                "question": section.question,
                "objectives": [target.objective_statement],
                "reviewMode": "reinforcement_verification",
                "remediationStrategy": "targeted_repair_then_unassisted_verify",
                "assessmentTargetId": target.id,
                "reinforcementRunId": run.id,
            }
            input_payload = {
                "request": request,
                "contentVersionId": content.id,
                "failedQuestionSignature": _question_signature(failed_question),
            }
            generation.context_hash = _hash(input_payload)
            result = await self.ai.lesson_quiz(
                request,
                _content_for_review_generation(content),
                [failed_question],
            )
            if len(result.questions) != 1:
                raise AppError("独立验证题数量无效", code="REINFORCEMENT_PACKAGE_INVALID", status=502)
            candidate = result.questions[0]
            candidate.objective = target.objective_statement
            candidate.core = bool(failed_question.get("core", False))
            alignment_reviewer = getattr(self.ai, "review_lesson_alignment", None)
            if not callable(alignment_reviewer):
                raise AppError("当前 AI 无法完成补强题对齐检查", code="REINFORCEMENT_ALIGNMENT_UNAVAILABLE", status=503, retryable=True)
            generated_content = _content_for_review_generation(content)
            alignment = await alignment_reviewer(
                request,
                generated_content,
                GeneratedQuiz(questions=[candidate]),
            )
            if not alignment.allowed:
                raise AppError("补强验证题与原正文未对齐", code="REINFORCEMENT_ALIGNMENT_FAILED", status=502, retryable=True)
            verify_question = candidate.model_dump()
            verify_question["assessmentTargetId"] = target.id
            verify_question["equivalenceGroupId"] = f"{target.id}:reinforcement:{run.id}"
            if not _questions_are_substantively_different(failed_question, verify_question):
                raise AppError("独立验证题与失败题实质重复", code="REINFORCEMENT_ITEM_NOT_NOVEL", status=502)
            verify_question = bind_remediation_questions_to_source_claims(
                self.db,
                content=content,
                questions=[verify_question],
                prior_questions=[failed_question],
            )[0]
            generation_index = self.db.scalar(
                select(func.max(QuizSet.generation)).where(QuizSet.section_id == section.id)
            ) or 0
            quiz = QuizSet(
                id=_uid("reinforcement_quiz"),
                section_id=section.id,
                content_version_id=content.id,
                learning_contract_version_id=assignment.learning_contract_version_id,
                generation=generation_index + 1,
                questions_json=_dump([verify_question]),
                schema_version=REINFORCEMENT_SCHEMA_VERSION,
            )
            self.db.add(quiz)
            self.db.flush()
            block_ids_by_position = {
                item.position: item.id
                for item in self.db.scalars(
                    select(ContentBlockVersion)
                    .where(ContentBlockVersion.content_version_id == content.id)
                    .order_by(ContentBlockVersion.position)
                ).all()
            }
            evidence_block_ids = [
                block_ids_by_position[index]
                for index in verify_question.get("claim_block_indexes", [])
                if index in block_ids_by_position
            ]
            verify_question = publish_assessment_item_versions(
                self.db,
                quiz=quiz,
                questions=[verify_question],
                evidence_block_ids_by_position=[evidence_block_ids],
                uid=_uid,
            )[0]
            governance = reevaluate_generated_governance(
                self.db,
                quiz_id=quiz.id,
                actor_id=run.id,
                actor_kind="reinforcement_run",
            )
            if not governance["allowed"] or not governance["assessmentEligible"]:
                raise AppError("补强验证题缺少可信正文绑定", code="REINFORCEMENT_GOVERNANCE_FAILED", status=409)

            package = ReinforcementPackageVersion(
                id=_uid("reinforcement_package"),
                run_id=run.id,
                generation_run_id=generation.id,
                verification_quiz_set_id=quiz.id,
                version=1,
                status="published",
                schema_version=REINFORCEMENT_SCHEMA_VERSION,
                prompt_version=REINFORCEMENT_PROMPT_VERSION,
                input_hash=_hash(input_payload),
                output_hash=_hash(verify_question),
            )
            self.db.add(package)
            self.db.flush()
            blocks = _load(content.blocks_json, [])
            relevant = [
                item for item in blocks
                if target.objective_statement in item.get("assessment_objectives", [])
                or target.id in item.get("assessmentTargetIds", [])
            ] or blocks[:2]
            repair_text = "\n\n".join(
                f"{item.get('heading', '关键线索')}：{item.get('content', '')}"
                for item in relevant[:2]
            )[:900]
            role_preferences = {
                "prerequisite_gap": {"prerequisite_scaffold", "core_instruction", "context"},
                "concept_confusion": {"comparison", "alternative_interpretation", "counterexample"},
                "mechanism_reasoning_break": {"mechanism", "derivation", "worked_example"},
                "boundary_comparison_error": {"boundary", "counterexample", "comparison"},
                "application_transfer_failure": {"application", "transfer", "empirical_case", "example"},
            }
            cases_by_cause = {}
            for cause_code, preferred_roles in role_preferences.items():
                case_block = next(
                    (item for item in relevant if item.get("role") in preferred_roles),
                    relevant[-1] if relevant else None,
                )
                if case_block:
                    cases_by_cause[cause_code] = {
                        "heading": case_block.get("heading") or "换一个角度检查",
                        "content": str(case_block.get("content") or "")[:900],
                        "source": "原教材中的已发布内容",
                    }
            activities = (
                ("diagnose", 1, "diagnose", "diagnostic", {
                    "heading": "Agent 先看作答证据",
                    "prompt": "下一步会按这条线索给出一个最小案例，再用新题检查判断是否成立。",
                    "hypothesis": {
                        "causeCode": diagnosis["causeCode"],
                        "label": diagnosis_label,
                        "status": diagnosis["status"],
                        "confidence": diagnosis["confidence"],
                        "evidenceCount": diagnosis["evidenceCount"],
                        "message": diagnosis_message,
                    },
                }, ""),
                ("repair", 2, "repair", "instructional", {
                    "heading": "把断点接回来",
                    "content": repair_text,
                    "casesByCause": cases_by_cause,
                    "prompt": "暂停十秒，用自己的话写下：这项能力成立的关键条件是什么？",
                }, ""),
                ("recompose", 3, "recompose", "run_only", {
                    "heading": "把原来的推理重新拼起来",
                    "question": failed_question,
                }, _question_signature(failed_question)),
                ("verify", 4, "verify", "formal_immediate", {
                    "heading": "最后独立验证一次",
                    "question": verify_question,
                }, _question_signature(verify_question)),
            )
            for key, position, activity_type, evidence_role, payload, signature in activities:
                self.db.add(ReinforcementActivityVersion(
                    id=_uid("reinforcement_activity"),
                    package_version_id=package.id,
                    assessment_target_id=target.id,
                    activity_key=key,
                    position=position,
                    activity_type=activity_type,
                    assistance_mode=("unassisted_reinforcement" if key == "verify" else "assisted_reinforcement"),
                    evidence_role=evidence_role,
                    payload_json=_dump(payload),
                    signature=signature,
                ))
            run.status = "active"
            run.current_state = "diagnose"
            run.current_activity_key = "diagnose"
            run.updated_at = now()
            generation.status = "succeeded"
            generation.finished_at = now()
            generation.trace_json = _dump({
                "reinforcementRunId": run.id,
                "packageVersionId": package.id,
                "verificationQuizSetId": quiz.id,
            })
            self.db.commit()
            return self.view(run.id)
        except Exception as error:
            self.db.rollback()
            failed_generation = self.db.get(GenerationRun, generation.id)
            if failed_generation:
                failed_generation.status = "failed"
                failed_generation.error_code = safe_error_code(error)
                failed_generation.error_message = str(error)[:2000]
                failed_generation.finished_at = now()
            failed_run = self.db.get(ReinforcementRun, run.id)
            if failed_run:
                failed_run.status = "replan_required"
                failed_run.current_state = "replan_required"
                failed_run.current_activity_key = ""
                failed_run.completed_at = now()
                failed_run.updated_at = now()
            self.db.commit()
            if isinstance(error, (AppError, AiError)):
                raise
            raise AppError("补强内容准备失败，请稍后重试", code="REINFORCEMENT_GENERATION_FAILED", status=502, retryable=True) from error

    async def start_for_target(self, target_id: str) -> dict:
        active = self.db.scalar(
            select(ReinforcementRun)
            .where(
                ReinforcementRun.user_id == self.user_id,
                ReinforcementRun.assessment_target_id == target_id,
                ReinforcementRun.status.in_({"preparing", "active"}),
            )
            .order_by(ReinforcementRun.updated_at.desc())
        )
        if active:
            return self.view(active.id)
        assignments = self.db.scalars(
            select(ReviewAssignment)
            .where(
                ReviewAssignment.user_id == self.user_id,
                ReviewAssignment.assessment_target_id == target_id,
                ReviewAssignment.status == "submitted",
                ReviewAssignment.submitted_attempt_id.is_not(None),
            )
            .order_by(ReviewAssignment.updated_at.desc())
        ).all()
        for assignment in assignments:
            attempt = self.db.get(QuizAttempt, assignment.submitted_attempt_id)
            if attempt and not attempt.passed:
                return await self.start_for_review(assignment.id)
        raise AppError(
            "先在复习中心完成一次新的无辅助唤醒，系统才能据此定位补强范围",
            code="REINFORCEMENT_WAKE_REQUIRED",
            status=409,
        )

    def respond(
        self,
        run_id: str,
        body,
        *,
        idempotency_key: str | None,
    ) -> dict:
        run = self._owned_run(run_id)
        if run.status != "active":
            return self.view(run.id)
        key = (idempotency_key or "").strip()
        if not 8 <= len(key) <= 128:
            raise AppError("补强请求标识无效", code="IDEMPOTENCY_KEY_INVALID", status=400)
        response = {
            "activityKey": body.activity_key,
            "selectedOptions": body.selected_options,
            "responseText": body.response_text,
            "acknowledged": body.acknowledged,
        }
        request_hash = _hash(response)
        replay = self.db.scalar(select(ReinforcementEventRecord).where(
            ReinforcementEventRecord.run_id == run.id,
            ReinforcementEventRecord.idempotency_key == key,
        ))
        if replay:
            if replay.request_hash != request_hash:
                raise AppError("请求标识已用于不同内容", code="IDEMPOTENCY_KEY_REUSED", status=409)
            return self.view(run.id, feedback=_load(replay.result_json, {}))
        if body.activity_key != run.current_activity_key:
            raise AppError("当前补强步骤已经变化，请刷新后继续", code="REINFORCEMENT_ACTIVITY_STALE", status=409)
        package = self._package(run.id)
        if not package:
            raise AppError("补强内容尚未准备完成", code="REINFORCEMENT_PACKAGE_MISSING", status=409)
        activity = self._activities(package.id).get(run.current_activity_key)
        if not activity:
            raise AppError("补强步骤不存在", code="REINFORCEMENT_ACTIVITY_MISSING", status=409)
        if run.activity_count >= run.max_activities:
            run.status = "replan_required"
            run.current_state = "replan_required"
            run.completed_at = now()
            self.db.commit()
            return self.view(run.id)

        state_before = run.current_state
        payload = _load(activity.payload_json, {})
        feedback: dict = {}
        observation_id = None
        if activity.activity_type == "diagnose":
            if not body.acknowledged:
                raise AppError("请先查看 Agent 的诊断线索", code="REINFORCEMENT_RESPONSE_INVALID", status=400)
            run.current_state = "repair"
            run.current_activity_key = "repair"
            feedback = {
                "kind": "diagnosed",
                "message": (
                    "已按作答证据缩小案例范围；这仍是待验证假设，不是你的画像结论。"
                    if run.confirmed_cause_code != INSUFFICIENT_EVIDENCE
                    else "证据不足时不猜原因；先从最小关键连接开始，再用后续作答判断。"
                ),
            }
        elif activity.activity_type == "repair":
            if not body.acknowledged and len(body.response_text.strip()) < 6:
                raise AppError("请先用自己的话写下一句再继续", code="REINFORCEMENT_RESPONSE_INVALID", status=400)
            if run.repair_rounds < 1:
                run.repair_rounds = 1
                run.current_state = "recompose"
                run.current_activity_key = "recompose"
            else:
                run.current_state = "verify"
                run.current_activity_key = "verify"
            feedback = {"kind": "instructional", "message": "这一步只帮助理解，不会被记成掌握证据。"}
        elif activity.activity_type == "recompose":
            try:
                grade = grade_choice_quiz([payload["question"]], [body.selected_options])
            except (KeyError, TypeError, ValueError) as error:
                raise AppError("答案格式无效", code="REINFORCEMENT_RESPONSE_INVALID", status=400) from error
            if grade.passed:
                run.current_state = "verify"
                run.current_activity_key = "verify"
                feedback = {"kind": "practice_correct", "message": "推理链已经重新拼好。下一题将独立验证，但本题不计入段位。"}
            else:
                run.repair_rounds = 2
                run.current_state = "repair"
                run.current_activity_key = "repair"
                feedback = {"kind": "practice_retry", "message": payload["question"].get("explanation", "再看一次关键条件。")}
        elif activity.activity_type == "verify":
            quiz = self.db.get(QuizSet, package.verification_quiz_set_id)
            questions = (
                immutable_questions_for_quiz(
                    self.db,
                    quiz,
                    require_versions=True,
                    require_evidence=True,
                )
                if quiz else []
            )
            governance = governance_view_for_quiz(self.db, quiz.id) if quiz else None
            if not quiz or not governance or not governance["allowed"] or not governance["assessmentEligible"]:
                raise AppError("独立验证题的可信状态已失效", code="REINFORCEMENT_GOVERNANCE_REQUIRED", status=409)
            try:
                grade = grade_choice_quiz(questions, [body.selected_options])
            except (KeyError, TypeError, ValueError) as error:
                raise AppError("答案格式无效", code="REINFORCEMENT_RESPONSE_INVALID", status=400) from error
            attempt = QuizAttempt(
                id=_uid("reinforcement_attempt"),
                quiz_set_id=quiz.id,
                learning_contract_version_id=run.learning_contract_version_id,
                content_version_id=run.content_version_id,
                learning_run_id=run.source_learning_run_id,
                user_id=self.user_id,
                idempotency_key=f"reinforcement:{run.id}:{key}"[:160],
                request_hash=request_hash,
                answers_json=_dump([body.selected_options]),
                results_json=_dump(grade.results),
                passed=grade.passed,
                workflow_status="succeeded",
            )
            self.db.add(attempt)
            self.db.flush()
            section = self.db.get(Section, run.source_section_id)
            record_scoring_facts(
                self.db,
                attempt=attempt,
                section=section,
                questions=questions,
                results=grade.results,
                score=grade.score,
                total=grade.total,
                passed=grade.passed,
                assistance_mode="unassisted_reinforcement",
                learning_episode_id=f"reinforcement:{run.id}:verify",
                qualification_profile="reinforcement_verification",
            )
            observation = self.db.scalar(select(AssessmentObservation).where(
                AssessmentObservation.attempt_id == attempt.id,
            ))
            observation_id = observation.id if observation else None
            run.status = "completed" if grade.passed else "replan_required"
            run.current_state = "complete" if grade.passed else "replan_required"
            run.current_activity_key = ""
            run.completed_at = now()
            feedback = {
                "kind": "verified" if grade.passed else "verification_failed",
                "correct": grade.passed,
                "message": (
                    "独立验证通过：掌握与段位可以据此重建；保持度仍要等下一次延迟唤醒。"
                    if grade.passed
                    else "独立验证仍未通过。本轮停止继续刷题，转为前置能力重规划。"
                ),
            }
        else:
            raise AppError("未知补强步骤", code="REINFORCEMENT_ACTIVITY_INVALID", status=409)

        run.activity_count += 1
        run.updated_at = now()
        event = ReinforcementEventRecord(
            id=_uid("reinforcement_event"),
            run_id=run.id,
            user_id=self.user_id,
            event_type="activity_submitted",
            activity_key=activity.activity_key,
            state_before=state_before,
            state_after=run.current_state,
            response_json=_dump(response),
            result_json=_dump(feedback),
            assistance_mode=activity.assistance_mode,
            source_observation_id=observation_id,
            idempotency_key=key,
            request_hash=request_hash,
            rule_version=REINFORCEMENT_STATE_RULE_VERSION,
        )
        self.db.add(event)
        self.db.commit()
        return self.view(run.id, feedback=feedback)
