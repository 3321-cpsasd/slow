"""Immutable high-stage delayed review task, submission, and evaluation facts."""

import hashlib
import json
import re
from difflib import SequenceMatcher
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...core.errors import AppError
from ...infrastructure.tables import (
    AssessmentObservation,
    AssessmentTarget,
    CapabilityConceptBinding,
    CapabilityRelationRequirement,
    CapabilityReviewEvaluation,
    CapabilityReviewSubmission,
    CapabilityReviewTaskVersion,
    CapabilityRevision,
    CapabilityStageCriterion,
    ConceptRevision,
    ContentBlockVersion,
    EvidenceQualificationEvent,
    KnowledgeRelationRevision,
    ReviewAssignment,
    now,
)
from .assessment import QUALIFICATION_RULE_VERSION, rebuild_assessment_projections
from .review_assignments import (
    ReviewAssignmentState,
    ReviewSubmission,
    qualify_retention_submission,
)


CAPABILITY_REVIEW_TASK_RULE_VERSION = "capability_review_task_v1"
CAPABILITY_REVIEW_EVALUATION_RULE_VERSION = "capability_review_evaluation_v1"
SUPPORTED_TASK_KINDS = {
    "oral_reactivation",
    "application_reactivation",
    "transfer_reactivation",
}


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def _dump(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _load(value: str, default):
    try:
        return json.loads(value) if value else default
    except (TypeError, ValueError):
        return default


def _hash(value: object) -> str:
    return hashlib.sha256(_dump(value).encode()).hexdigest()


def _normalized(value: str) -> str:
    return re.sub(r"\s+", "", value).lower()


class CapabilityReviewTaskService:
    def __init__(self, db: Session, *, user_id: str, ai):
        self.db = db
        self.user_id = user_id
        self.ai = ai

    @staticmethod
    def _lineage(ai) -> tuple[str, str, str]:
        return (
            str(getattr(ai, "last_deployment_id", "") or ""),
            str(getattr(ai, "last_model_family_id", "") or ""),
            str(getattr(ai, "model", "") or ""),
        )

    def _existing(self, assignment_id: str) -> CapabilityReviewTaskVersion | None:
        return self.db.scalar(
            select(CapabilityReviewTaskVersion).where(
                CapabilityReviewTaskVersion.review_assignment_id == assignment_id
            )
        )

    @staticmethod
    def view(task: CapabilityReviewTaskVersion) -> dict:
        return {
            "id": task.id,
            "schemaVersion": "capability_review_task_v1",
            "taskKind": task.task_kind,
            "stage": task.stage,
            "prompt": task.prompt,
            "taskContext": _load(task.task_context_json, {}),
            "deliverables": _load(task.deliverables_json, []),
            "status": "ready",
            "evidenceEligible": task.publication_status == "published",
            "isDemo": task.provenance_mode == "local_demo",
        }

    def view_for_assignment(self, assignment_id: str) -> dict | None:
        task = self._existing(assignment_id)
        return self.view(task) if task else None

    def _required_knowledge(self, capability_id: str) -> list[dict]:
        concepts = self.db.execute(
            select(CapabilityConceptBinding, ConceptRevision)
            .join(
                ConceptRevision,
                ConceptRevision.id == CapabilityConceptBinding.concept_revision_id,
            )
            .where(
                CapabilityConceptBinding.capability_revision_id == capability_id,
                CapabilityConceptBinding.required.is_(True),
            )
            .order_by(CapabilityConceptBinding.position)
        ).all()
        relations = self.db.execute(
            select(CapabilityRelationRequirement, KnowledgeRelationRevision)
            .join(
                KnowledgeRelationRevision,
                KnowledgeRelationRevision.id
                == CapabilityRelationRequirement.knowledge_relation_revision_id,
            )
            .where(
                CapabilityRelationRequirement.capability_revision_id == capability_id,
                CapabilityRelationRequirement.required.is_(True),
            )
            .order_by(CapabilityRelationRequirement.position)
        ).all()
        return [
            {"kind": "concept", "id": revision.id, "label": revision.label}
            for _binding, revision in concepts
        ] + [
            {"kind": "relation", "id": revision.id, "label": revision.statement}
            for _requirement, revision in relations
        ]

    @staticmethod
    def _validate_candidate(candidate, *, criterion_ids: set[str], task_kind: str, blocks):
        if any(marker in candidate.prompt for marker in ("A.", "B.", "A、", "B、")):
            raise AppError(
                "能力再激活任务不能退化成选择题",
                code="CAPABILITY_REVIEW_CHOICE_FORMAT_INVALID",
                status=409,
            )
        prompt = _normalized(candidate.prompt)
        for block in blocks:
            taught = _normalized(f"{block.heading}{block.content}")
            if not taught:
                continue
            shorter, longer = sorted((prompt, taught), key=len)
            if (
                shorter in longer and len(shorter) >= 40
            ) or SequenceMatcher(None, prompt, taught).ratio() >= 0.82:
                raise AppError(
                    "能力再激活任务复用了正文题面",
                    code="CAPABILITY_REVIEW_TASK_NOT_NOVEL",
                    status=409,
                )
        rubric_criterion_ids = {item.stage_criterion_id for item in candidate.rubric}
        if rubric_criterion_ids != criterion_ids or not all(
            item.required for item in candidate.rubric
        ):
            raise AppError(
                "能力再激活任务没有完整覆盖冻结阶段量规",
                code="CAPABILITY_REVIEW_RUBRIC_COVERAGE_INVALID",
                status=409,
            )
        if task_kind == "transfer_reactivation" and (
            len(candidate.required_knowledge_recombination) < 2
            or not candidate.unfamiliarity_basis.strip()
        ):
            raise AppError(
                "钻石再激活任务缺少陌生情境或知识重组",
                code="CAPABILITY_REVIEW_TRANSFER_REQUIREMENTS_INVALID",
                status=409,
            )

    async def prepare(self, assignment: ReviewAssignment) -> CapabilityReviewTaskVersion:
        existing = self._existing(assignment.id)
        if existing:
            return existing
        plan = _load(assignment.task_plan_json, {})
        reactivation = plan.get("reactivation", {})
        task_kind = reactivation.get("taskKind", "")
        if task_kind not in SUPPORTED_TASK_KINDS:
            raise AppError(
                "复习任务没有可执行的高阶段协议",
                code="CAPABILITY_REVIEW_TASK_KIND_INVALID",
                status=409,
            )
        criterion_ids = list(reactivation.get("criterionIds", []))
        criteria = self.db.scalars(
            select(CapabilityStageCriterion).where(
                CapabilityStageCriterion.id.in_(criterion_ids)
            )
        ).all()
        target = self.db.get(AssessmentTarget, assignment.assessment_target_id)
        capability = (
            self.db.get(CapabilityRevision, target.capability_revision_id)
            if target and target.capability_revision_id
            else None
        )
        if (
            target is None
            or capability is None
            or len(criteria) != len(criterion_ids)
            or {item.capability_revision_id for item in criteria} != {capability.id}
            or {item.stage for item in criteria} != {reactivation.get("stage")}
        ):
            raise AppError(
                "复习任务的能力阶段绑定已经失效",
                code="CAPABILITY_REVIEW_STAGE_BINDING_INVALID",
                status=409,
            )
        blocks = self.db.scalars(
            select(ContentBlockVersion)
            .where(ContentBlockVersion.content_version_id == assignment.content_version_id)
            .order_by(ContentBlockVersion.position)
        ).all()
        if not blocks:
            raise AppError(
                "能力复习缺少已发布正文块",
                code="CAPABILITY_REVIEW_CONTENT_MISSING",
                status=409,
            )
        required_knowledge = self._required_knowledge(capability.id)
        if task_kind == "transfer_reactivation" and len(required_knowledge) < 2:
            raise AppError(
                "钻石复习缺少可重组的能力知识子网",
                code="CAPABILITY_REVIEW_SUBNET_INSUFFICIENT",
                status=409,
            )
        request = {
            "schemaVersion": "capability_review_author_request_v1",
            "taskKind": task_kind,
            "stage": reactivation["stage"],
            "plannedCriteria": [
                {
                    "id": item.id,
                    "statement": item.statement,
                    "taskType": item.task_type,
                    "contextRequirement": item.context_requirement,
                }
                for item in sorted(criteria, key=lambda item: item.position)
            ],
            "capability": {
                "id": capability.id,
                "label": capability.label,
                "scope": _load(capability.scope_json, {}),
            },
            "requiredKnowledge": required_knowledge,
            "publishedContentBlocks": [
                {
                    "id": item.id,
                    "role": item.semantic_role,
                    "heading": item.heading,
                    "content": item.content,
                }
                for item in blocks
            ],
        }
        candidate = await self.ai.author_capability_review_task(request)
        self._validate_candidate(
            candidate,
            criterion_ids=set(criterion_ids),
            task_kind=task_kind,
            blocks=blocks,
        )
        if task_kind == "transfer_reactivation":
            labels = {item["label"] for item in required_knowledge}
            if not set(candidate.required_knowledge_recombination).issubset(labels):
                raise AppError(
                    "钻石复习重组了能力子网之外的知识",
                    code="CAPABILITY_REVIEW_RECOMBINATION_OUTSIDE_SUBNET",
                    status=409,
                )
        deployment, family, model = self._lineage(self.ai)
        formal = bool(getattr(self.ai, "configured", False))
        if formal and (not deployment or not family):
            raise AppError(
                "能力复习任务缺少作者模型血缘",
                code="CAPABILITY_REVIEW_AUTHOR_LINEAGE_MISSING",
                status=409,
            )
        rubric = [
            {
                "criterionKey": item.criterion_key,
                "stageCriterionId": item.stage_criterion_id,
                "statement": item.statement,
                "required": item.required,
            }
            for item in candidate.rubric
        ]
        payload = {
            "assignmentId": assignment.id,
            "taskKind": task_kind,
            "stage": reactivation["stage"],
            "criterionIds": criterion_ids,
            "prompt": candidate.prompt,
            "context": candidate.task_context,
            "rubric": rubric,
            "requiredKnowledge": candidate.required_knowledge_recombination,
        }
        task = CapabilityReviewTaskVersion(
            id=_uid("capability_review_task"),
            review_assignment_id=assignment.id,
            assessment_target_id=assignment.assessment_target_id,
            capability_revision_id=capability.id,
            task_kind=task_kind,
            stage=reactivation["stage"],
            criterion_ids_json=_dump(criterion_ids),
            prompt=candidate.prompt,
            task_context_json=_dump({"scenario": candidate.task_context}),
            deliverables_json=_dump(candidate.deliverables),
            rubric_json=_dump(rubric),
            reference_answer_json=_dump(candidate.reference_answer_points),
            novelty_basis_json=_dump({
                "novelty": candidate.novelty_basis,
                "unfamiliarity": candidate.unfamiliarity_basis,
            }),
            required_knowledge_json=_dump(
                candidate.required_knowledge_recombination
            ),
            author_deployment_id=deployment,
            author_model_family_id=family,
            author_model=model,
            provenance_mode="ai_authored" if formal else "local_demo",
            publication_status="published" if formal else "published_demo",
            task_hash=_hash(payload),
            rule_version=CAPABILITY_REVIEW_TASK_RULE_VERSION,
        )
        self.db.add(task)
        self.db.flush()
        return task

    @staticmethod
    def _validate_evaluation(evaluation, rubric: list[dict]) -> None:
        expected = {item["criterionKey"] for item in rubric}
        actual = {item.criterion_key for item in evaluation.criterion_results}
        if actual != expected or len(actual) != len(evaluation.criterion_results):
            raise AppError(
                "能力复习评定没有完整覆盖冻结量规",
                code="CAPABILITY_REVIEW_EVALUATION_COVERAGE_INVALID",
                status=409,
            )
        required = {
            item["criterionKey"] for item in rubric if item.get("required", True)
        }
        satisfied = {
            item.criterion_key
            for item in evaluation.criterion_results
            if item.satisfied
        }
        valid_pass = (
            evaluation.evidence_sufficiency == "sufficient"
            and required.issubset(satisfied)
        )
        if (evaluation.verdict == "pass") != valid_pass:
            raise AppError(
                "能力复习总评与逐项量规不一致",
                code="CAPABILITY_REVIEW_EVALUATION_VERDICT_INVALID",
                status=409,
            )

    async def submit(
        self,
        assignment: ReviewAssignment,
        *,
        response: dict,
        assistance_used: bool,
        idempotency_key: str,
    ) -> tuple[dict, CapabilityReviewSubmission]:
        task = self._existing(assignment.id)
        if task is None:
            raise AppError(
                "能力复习任务尚未开始",
                code="CAPABILITY_REVIEW_TASK_NOT_STARTED",
                status=409,
            )
        request_key = idempotency_key.strip()
        if not 8 <= len(request_key) <= 128:
            raise AppError("请求标识无效", code="IDEMPOTENCY_KEY_INVALID", status=400)
        if not response:
            raise AppError(
                "能力复习提交不能为空",
                code="CAPABILITY_REVIEW_SUBMISSION_EMPTY",
                status=400,
            )
        request_hash = _hash({
            "taskId": task.id,
            "response": response,
            "assistanceUsed": assistance_used,
        })
        replay = self.db.scalar(
            select(CapabilityReviewSubmission).where(
                CapabilityReviewSubmission.review_task_version_id == task.id,
            )
        )
        if replay:
            if (
                replay.idempotency_key != request_key
                or replay.request_hash != request_hash
            ):
                raise AppError(
                    "请求标识已用于其他能力复习提交",
                    code="IDEMPOTENCY_KEY_REUSED",
                    status=409,
                )
            return _load(replay.result_json, {}), replay
        submission = CapabilityReviewSubmission(
            id=_uid("capability_review_submission"),
            review_task_version_id=task.id,
            user_id=self.user_id,
            idempotency_key=request_key,
            request_hash=request_hash,
            response_json=_dump(response),
            assistance_mode=(
                "declared_assisted" if assistance_used else "unassisted_review"
            ),
            status="processing",
        )
        self.db.add(submission)
        self.db.flush()
        rubric = _load(task.rubric_json, [])
        evaluation_candidate = await self.ai.evaluate_capability_review_submission(
            {
                "schemaVersion": "capability_review_evaluation_request_v1",
                "task": {
                    "id": task.id,
                    "taskKind": task.task_kind,
                    "stage": task.stage,
                    "prompt": task.prompt,
                    "taskContext": _load(task.task_context_json, {}),
                    "requiredKnowledge": _load(task.required_knowledge_json, []),
                },
                "rubric": rubric,
                "referenceAnswerPoints": _load(task.reference_answer_json, []),
                "submission": response,
                "authorDeploymentId": task.author_deployment_id,
                "authorModelFamilyId": task.author_model_family_id,
                "authorModel": task.author_model,
            }
        )
        self._validate_evaluation(evaluation_candidate, rubric)
        evaluator_deployment, evaluator_family, evaluator_model = self._lineage(self.ai)
        independent = bool(
            task.author_model_family_id
            and evaluator_family
            and task.author_model_family_id != evaluator_family
        )
        retention_qualification = qualify_retention_submission(
            ReviewAssignmentState(
                assignment_id=assignment.id,
                user_id=assignment.user_id,
                assessment_target_id=assignment.assessment_target_id,
                due_at=assignment.due_at,
                expires_at=assignment.expires_at,
                status=assignment.status,
                last_event_at=assignment.last_event_at,
                rule_version=assignment.selection_rule_version,
            ),
            ReviewSubmission(
                assignment_id=assignment.id,
                assessment_target_id=assignment.assessment_target_id,
                submitted_at=now(),
                assistance_mode=submission.assistance_mode,
                item_signatures=frozenset({task.task_hash}),
                prior_item_signatures=frozenset(
                    _load(assignment.prior_item_signatures_json, [])
                ),
            ),
        )
        formal_evaluable = bool(
            task.publication_status == "published"
            and task.provenance_mode == "ai_authored"
            and not assistance_used
            and independent
            and retention_qualification.eligible
        )
        passed = (
            evaluation_candidate.verdict == "pass"
            and evaluation_candidate.evidence_sufficiency == "sufficient"
        )
        qualified = formal_evaluable and passed
        reason = (
            "qualified_delayed_unassisted_independent_reactivation"
            if qualified
            else "declared_assistance_used"
            if assistance_used
            else "task_is_demo_or_not_formally_published"
            if task.publication_status != "published"
            or task.provenance_mode != "ai_authored"
            else "evaluation_not_independent"
            if not independent
            else "retention_assignment_ineligible"
            if not retention_qualification.eligible
            else "evaluation_did_not_pass_with_sufficient_evidence"
        )
        evaluation = CapabilityReviewEvaluation(
            id=_uid("capability_review_evaluation"),
            submission_id=submission.id,
            verdict=evaluation_candidate.verdict,
            evidence_sufficiency=evaluation_candidate.evidence_sufficiency,
            criterion_results_json=_dump([
                {
                    "criterionKey": item.criterion_key,
                    "satisfied": item.satisfied,
                    "rationale": item.rationale,
                }
                for item in evaluation_candidate.criterion_results
            ]),
            rationale=evaluation_candidate.rationale,
            evaluator_deployment_id=evaluator_deployment,
            evaluator_model_family_id=evaluator_family,
            evaluator_model=evaluator_model,
            qualification_status="eligible" if qualified else "ineligible",
            qualification_reason=reason,
            rule_version=CAPABILITY_REVIEW_EVALUATION_RULE_VERSION,
        )
        self.db.add(evaluation)
        self.db.flush()
        observation = AssessmentObservation(
            id=_uid("observation"),
            learning_run_id=assignment.source_learning_run_id,
            user_id=self.user_id,
            section_id=assignment.source_section_id,
            attempt_id=None,
            quiz_set_id=None,
            learning_contract_version_id=assignment.learning_contract_version_id,
            content_version_id=assignment.content_version_id,
            scoring_result_id=None,
            assessment_target_id=assignment.assessment_target_id,
            question_index=None,
            correct=passed,
            source_type="capability_review",
            evidence_key=hashlib.sha256(
                f"capability_review:{submission.id}:{task.id}".encode()
            ).hexdigest(),
            assistance_mode=submission.assistance_mode,
            learning_episode_id=f"capability_review:{submission.id}",
            equivalence_group_id=task.task_hash,
            qualification_at_creation=(
                "eligible_grouped" if formal_evaluable else "ineligible"
            ),
            qualification_rule_version=QUALIFICATION_RULE_VERSION,
            payload_json=_dump({
                "taskVersionId": task.id,
                "submissionId": submission.id,
                "evaluationId": evaluation.id,
                "questionFingerprint": task.task_hash,
                "evidenceEffect": "activation_only",
            }),
        )
        self.db.add(observation)
        self.db.flush()
        statuses = {
            "gate": ("ineligible", "delayed capability review cannot rewrite gate"),
            "mastery": (
                "ineligible",
                "capability reactivation cannot rewrite concept mastery",
            ),
            "retention": (
                "candidate" if qualified else "ineligible",
                "qualified delayed reactivation" if qualified else reason,
            ),
            "rank": ("ineligible", "reactivation cannot change legacy rank"),
            "capability": (
                "ineligible",
                "reactivation cannot satisfy or replace stage criteria",
            ),
        }
        for family, (status, event_reason) in statuses.items():
            self.db.add(
                EvidenceQualificationEvent(
                    id=_uid("qualification"),
                    observation_id=observation.id,
                    projection_family=family,
                    status=status,
                    reason=event_reason,
                    rule_version=QUALIFICATION_RULE_VERSION,
                )
            )
        self.db.flush()
        rebuild_assessment_projections(self.db, user_id=self.user_id)
        result = {
            "schemaVersion": "capability_review_result_v1",
            "assignmentId": assignment.id,
            "submissionId": submission.id,
            "status": "submitted",
            "verdict": evaluation.verdict,
            "evidenceSufficiency": evaluation.evidence_sufficiency,
            "reactivationQualified": qualified,
            "stageChanged": False,
            "feedback": (
                "当前能力已重新确认可调用。"
                if qualified
                else "本次表现尚不足以重新确认当前能力。"
            ),
        }
        submission.status = "completed"
        submission.result_json = _dump(result)
        return result, submission
