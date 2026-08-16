import hashlib
import json
import re
from difflib import SequenceMatcher
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ...core.errors import AppError
from ...infrastructure.tables import (
    AssessmentObservation,
    AssessmentTarget,
    CapabilityApplicationEvaluation,
    CapabilityApplicationSubmission,
    CapabilityApplicationTaskVersion,
    CapabilityRevision,
    CapabilityStageCriterion,
    CapabilityStateProjection,
    ContentBlockVersion,
    ContentVersion,
    EvidenceQualificationEvent,
    LearningContractAssessmentTarget,
    LearningContractVersion,
    LearningRunSectionBinding,
)
from .assessment import QUALIFICATION_RULE_VERSION, rebuild_assessment_projections
from .capabilities import publish_standard_application_opportunity


APPLICATION_AUTHORING_RULE_VERSION = "standard_application_authoring_v1"
APPLICATION_EVALUATION_RULE_VERSION = "standard_application_evaluation_v1"


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def _dump(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _load(value: str, default):
    try:
        return json.loads(value) if value else default
    except json.JSONDecodeError:
        return default


def _hash(value: object) -> str:
    return hashlib.sha256(_dump(value).encode()).hexdigest()


def _normalized(value: str) -> str:
    return re.sub(r"\s+", "", value).lower()


class CapabilityApplicationTaskService:
    """Owns the published gold task -> submission -> evaluation fact chain."""

    def __init__(self, db: Session, *, user_id: str, ai, contexts, progress):
        self.db = db
        self.user_id = user_id
        self.ai = ai
        self.contexts = contexts
        self.progress = progress

    def _binding(self, run_id: str, section_id: str) -> LearningRunSectionBinding:
        binding = self.db.scalar(
            select(LearningRunSectionBinding).where(
                LearningRunSectionBinding.learning_run_id == run_id,
                LearningRunSectionBinding.user_id == self.user_id,
                LearningRunSectionBinding.section_id == section_id,
            )
        )
        if binding is None:
            raise AppError(
                "标准应用任务需要先完成小节内容生成",
                code="APPLICATION_TASK_SECTION_BINDING_MISSING",
                status=409,
            )
        return binding

    def _target(
        self, contract_id: str
    ) -> tuple[LearningContractAssessmentTarget, AssessmentTarget]:
        rows = self.db.execute(
            select(LearningContractAssessmentTarget, AssessmentTarget)
            .join(
                AssessmentTarget,
                AssessmentTarget.id
                == LearningContractAssessmentTarget.assessment_target_id,
            )
            .where(
                LearningContractAssessmentTarget.contract_version_id == contract_id,
                LearningContractAssessmentTarget.diagnostic_only.is_(True),
                LearningContractAssessmentTarget.verification_policy
                == "standard_application_v1",
                AssessmentTarget.dimension == "application",
            )
        ).all()
        if len(rows) != 1:
            raise AppError(
                "学习契约没有唯一的标准应用能力目标",
                code="APPLICATION_TASK_CONTRACT_TARGET_INVALID",
                status=409,
            )
        binding, target = rows[0]
        criterion = (
            self.db.get(
                CapabilityStageCriterion,
                target.capability_stage_criterion_id,
            )
            if target.capability_stage_criterion_id
            else None
        )
        if (
            not target.capability_revision_id
            or criterion is None
            or criterion.capability_revision_id != target.capability_revision_id
            or criterion.stage != "gold"
            or criterion.task_type != "standard_application"
            or criterion.novelty_requirement != "unseen"
            or criterion.assistance_limit != "unassisted"
            or criterion.verification_protocol != "standard_application_v1"
        ):
            raise AppError(
                "标准应用目标与黄金阶段量规不一致",
                code="APPLICATION_TASK_CRITERION_PROTOCOL_INVALID",
                status=409,
            )
        return binding, target

    def _existing(
        self, contract_id: str, criterion_id: str
    ) -> CapabilityApplicationTaskVersion | None:
        return self.db.scalar(
            select(CapabilityApplicationTaskVersion)
            .where(
                CapabilityApplicationTaskVersion.learning_contract_version_id
                == contract_id,
                CapabilityApplicationTaskVersion.capability_stage_criterion_id
                == criterion_id,
                CapabilityApplicationTaskVersion.publication_status.in_(
                    ("published", "published_demo")
                ),
            )
            .order_by(CapabilityApplicationTaskVersion.version.desc())
        )

    @staticmethod
    def _lineage(ai) -> tuple[str, str, str]:
        return (
            str(getattr(ai, "last_deployment_id", "") or ""),
            str(getattr(ai, "last_model_family_id", "") or ""),
            str(getattr(ai, "model", "") or ""),
        )

    @staticmethod
    def _task_view(task: CapabilityApplicationTaskVersion) -> dict:
        return {
            "id": task.id,
            "schemaVersion": "standard_application_task_v1",
            "prompt": task.prompt,
            "taskContext": _load(task.task_context_json, {}),
            "deliverables": _load(task.deliverables_json, []),
            "status": "ready",
            "evidenceEligible": task.publication_status == "published",
            "isDemo": task.provenance_mode == "local_demo",
        }

    def view(self, section_id: str) -> dict:
        context = self.contexts.resolve_section(
            user_id=self.user_id, section_id=section_id
        )
        run = self.progress.active_run(context.series.id)
        binding = self._binding(run.id, section_id)
        _contract_binding, target = self._target(
            binding.learning_contract_version_id
        )
        task = self._existing(
            binding.learning_contract_version_id,
            target.capability_stage_criterion_id,
        )
        if task is None:
            raise AppError(
                "标准应用任务还没有准备好",
                code="APPLICATION_TASK_NOT_PREPARED",
                status=404,
            )
        return self._task_view(task)

    @staticmethod
    def _validate_candidate(candidate, blocks: list[ContentBlockVersion]) -> None:
        prompt = _normalized(candidate.prompt)
        if any(marker in candidate.prompt for marker in ("A.", "B.", "A、", "B、")):
            raise AppError(
                "标准应用任务不能伪装成选择题",
                code="APPLICATION_TASK_CHOICE_FORMAT_INVALID",
                status=409,
            )
        if len(prompt) < 20:
            raise AppError(
                "标准应用任务信息不足",
                code="APPLICATION_TASK_PROMPT_INSUFFICIENT",
                status=409,
            )
        for block in blocks:
            taught = _normalized(f"{block.heading}{block.content}")
            if not taught:
                continue
            shorter, longer = sorted((prompt, taught), key=len)
            copied = shorter in longer and len(shorter) >= 40
            similarity = SequenceMatcher(None, prompt, taught).ratio()
            if copied or similarity >= 0.82:
                raise AppError(
                    "标准应用任务复用了正文中的已见题面",
                    code="APPLICATION_TASK_NOT_NOVEL",
                    status=409,
                )
        if not all(item.required for item in candidate.rubric):
            raise AppError(
                "黄金任务量规不能包含可跳过标准",
                code="APPLICATION_TASK_RUBRIC_OPTIONAL_INVALID",
                status=409,
            )

    async def prepare(self, section_id: str) -> dict:
        context = self.contexts.resolve_section(
            user_id=self.user_id, section_id=section_id
        )
        run = self.progress.active_run(context.series.id)
        binding = self._binding(run.id, section_id)
        contract = self.db.get(
            LearningContractVersion, binding.learning_contract_version_id
        )
        content = self.db.get(ContentVersion, binding.content_version_id)
        if (
            contract is None
            or content is None
            or content.publication_status != "published"
            or content.learning_contract_version_id != contract.id
        ):
            raise AppError(
                "标准应用任务缺少已发布且契约一致的正文",
                code="APPLICATION_TASK_CONTENT_BOUNDARY_INVALID",
                status=409,
            )
        _contract_binding, target = self._target(contract.id)
        existing = self._existing(
            contract.id, target.capability_stage_criterion_id
        )
        if existing:
            return self._task_view(existing)

        criterion = self.db.get(
            CapabilityStageCriterion, target.capability_stage_criterion_id
        )
        capability = self.db.get(CapabilityRevision, target.capability_revision_id)
        blocks = self.db.scalars(
            select(ContentBlockVersion)
            .where(ContentBlockVersion.content_version_id == content.id)
            .order_by(ContentBlockVersion.position)
        ).all()
        if not blocks:
            raise AppError(
                "标准应用任务缺少可比较的已发布正文块",
                code="APPLICATION_TASK_CONTENT_BLOCKS_MISSING",
                status=409,
            )
        candidate = await self.ai.author_standard_application_task(
            {
                "schemaVersion": "standard_application_author_request_v1",
                "learningContract": {
                    "id": contract.id,
                    "question": contract.section_question_snapshot,
                    "targetDepth": contract.target_depth,
                },
                "capability": {
                    "id": capability.id,
                    "label": capability.label,
                    "scope": _load(capability.scope_json, {}),
                },
                "criterion": {
                    "id": criterion.id,
                    "statement": criterion.statement,
                    "taskType": criterion.task_type,
                    "noveltyRequirement": criterion.novelty_requirement,
                    "assistanceLimit": criterion.assistance_limit,
                    "contextRequirement": criterion.context_requirement,
                },
                "publishedContentBlocks": [
                    {
                        "blockId": item.id,
                        "role": item.semantic_role,
                        "heading": item.heading,
                        "content": item.content,
                    }
                    for item in blocks
                ],
            }
        )
        self._validate_candidate(candidate, blocks)
        author_deployment, author_family, author_model = self._lineage(self.ai)
        formal = bool(getattr(self.ai, "configured", False))
        if formal and (not author_deployment or not author_family):
            raise AppError(
                "标准应用任务缺少模型作者血缘",
                code="APPLICATION_TASK_AUTHOR_LINEAGE_MISSING",
                status=409,
            )
        rubric = [
            {
                "criterionKey": item.criterion_key,
                "statement": item.statement,
                "required": item.required,
            }
            for item in candidate.rubric
        ]
        task_payload = {
            "contractId": contract.id,
            "contentVersionId": content.id,
            "targetId": target.id,
            "criterionId": criterion.id,
            "prompt": candidate.prompt,
            "taskContext": candidate.task_context,
            "deliverables": candidate.deliverables,
            "rubric": rubric,
            "referenceAnswerPoints": candidate.reference_answer_points,
            "noveltyBasis": candidate.novelty_basis,
            "authoringRuleVersion": APPLICATION_AUTHORING_RULE_VERSION,
        }
        version = (
            self.db.scalar(
                select(func.max(CapabilityApplicationTaskVersion.version)).where(
                    CapabilityApplicationTaskVersion.learning_contract_version_id
                    == contract.id,
                    CapabilityApplicationTaskVersion.capability_stage_criterion_id
                    == criterion.id,
                )
            )
            or 0
        ) + 1
        task = CapabilityApplicationTaskVersion(
            id=_uid("capability_application_task"),
            section_id=section_id,
            learning_contract_version_id=contract.id,
            content_version_id=content.id,
            assessment_target_id=target.id,
            capability_revision_id=capability.id,
            capability_stage_criterion_id=criterion.id,
            version=version,
            prompt=candidate.prompt,
            task_context_json=_dump({"scenario": candidate.task_context}),
            deliverables_json=_dump(candidate.deliverables),
            rubric_json=_dump(rubric),
            reference_answer_json=_dump(candidate.reference_answer_points),
            novelty_basis_json=_dump({
                "authorClaim": candidate.novelty_basis,
                "deterministicCheck": "normalized_copy_and_similarity_v1",
                "comparedContentBlockIds": [item.id for item in blocks],
            }),
            author_deployment_id=author_deployment,
            author_model_family_id=author_family,
            author_model=author_model,
            provenance_mode="ai_authored" if formal else "local_demo",
            publication_status="published" if formal else "published_demo",
            task_hash=_hash(task_payload),
            authoring_rule_version=APPLICATION_AUTHORING_RULE_VERSION,
        )
        self.db.add(task)
        self.db.flush()
        if formal:
            publish_standard_application_opportunity(
                self.db,
                series_id=context.series.id,
                capability_revision_id=capability.id,
                task_version_id=task.id,
            )
        self.db.commit()
        return self._task_view(task)

    @staticmethod
    def _validate_evaluation(evaluation, rubric: list[dict]) -> None:
        expected = {item["criterionKey"] for item in rubric}
        actual = {item.criterion_key for item in evaluation.criterion_results}
        if actual != expected or len(actual) != len(evaluation.criterion_results):
            raise AppError(
                "标准应用任务评定没有完整覆盖冻结量规",
                code="APPLICATION_EVALUATION_RUBRIC_COVERAGE_INVALID",
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
                "标准应用任务总评与逐项量规不一致",
                code="APPLICATION_EVALUATION_VERDICT_INVALID",
                status=409,
            )

    def _record_observation(
        self,
        *,
        task: CapabilityApplicationTaskVersion,
        submission: CapabilityApplicationSubmission,
        evaluation: CapabilityApplicationEvaluation,
        qualified: bool,
    ) -> AssessmentObservation:
        observation = AssessmentObservation(
            id=_uid("observation"),
            learning_run_id=submission.learning_run_id,
            user_id=self.user_id,
            section_id=task.section_id,
            attempt_id=None,
            quiz_set_id=None,
            learning_contract_version_id=task.learning_contract_version_id,
            content_version_id=task.content_version_id,
            scoring_result_id=None,
            assessment_target_id=task.assessment_target_id,
            question_index=None,
            correct=evaluation.verdict == "pass",
            source_type="standard_application",
            evidence_key=hashlib.sha256(
                f"standard_application:{submission.id}:{task.assessment_target_id}".encode()
            ).hexdigest(),
            assistance_mode=submission.assistance_mode,
            learning_episode_id=f"standard_application:{submission.id}",
            equivalence_group_id=hashlib.sha256(task.task_hash.encode()).hexdigest(),
            qualification_at_creation=(
                "eligible_grouped" if qualified else "ineligible"
            ),
            qualification_rule_version=QUALIFICATION_RULE_VERSION,
            payload_json=_dump({
                "taskVersionId": task.id,
                "submissionId": submission.id,
                "evaluationId": evaluation.id,
                "verdict": evaluation.verdict,
                "evidenceSufficiency": evaluation.evidence_sufficiency,
                "evaluationRuleVersion": evaluation.evaluation_rule_version,
            }),
        )
        self.db.add(observation)
        self.db.flush()
        statuses = {
            "gate": ("ineligible", "application task cannot rewrite the section gate"),
            "mastery": (
                "eligible_grouped" if qualified else "ineligible",
                "qualified application task may update mastery" if qualified else "application task did not qualify",
            ),
            "retention": ("ineligible", "same-episode application is not delayed retention"),
            "rank": ("ineligible", "new capability stages replace legacy rank progression"),
            "capability": (
                "eligible_grouped" if qualified else "ineligible",
                "qualified unseen unassisted standard task satisfies gold criterion" if qualified else evaluation.qualification_reason,
            ),
        }
        for family, (status, reason) in statuses.items():
            self.db.add(
                EvidenceQualificationEvent(
                    id=_uid("qualification"),
                    observation_id=observation.id,
                    projection_family=family,
                    status=status,
                    reason=reason,
                    rule_version=QUALIFICATION_RULE_VERSION,
                )
            )
        self.db.flush()
        return observation

    async def submit(
        self,
        task_id: str,
        *,
        response: dict,
        assistance_used: bool,
        idempotency_key: str,
    ) -> dict:
        request_key = (idempotency_key or "").strip()
        if not 8 <= len(request_key) <= 128:
            raise AppError(
                "标准应用任务请求标识无效",
                code="IDEMPOTENCY_KEY_INVALID",
                status=400,
            )
        if not response:
            raise AppError(
                "标准应用任务提交不能为空",
                code="APPLICATION_SUBMISSION_EMPTY",
                status=400,
            )
        task = self.db.get(CapabilityApplicationTaskVersion, task_id)
        if task is None:
            raise AppError(
                "标准应用任务不存在",
                code="APPLICATION_TASK_NOT_FOUND",
                status=404,
            )
        context = self.contexts.resolve_section(
            user_id=self.user_id, section_id=task.section_id
        )
        run = self.progress.active_run(context.series.id)
        binding = self._binding(run.id, task.section_id)
        if (
            binding.learning_contract_version_id != task.learning_contract_version_id
            or binding.content_version_id != task.content_version_id
        ):
            raise AppError(
                "标准应用任务不属于当前学习实例",
                code="APPLICATION_TASK_RUN_BINDING_INVALID",
                status=409,
            )
        state_before = self.db.scalar(
            select(CapabilityStateProjection).where(
                CapabilityStateProjection.user_id == self.user_id,
                CapabilityStateProjection.capability_revision_id
                == task.capability_revision_id,
            )
        )
        if state_before is None or state_before.current_stage not in {
            "silver",
            "gold",
            "diamond",
        }:
            raise AppError(
                "请先完成讲清机制与边界的能力验证",
                code="APPLICATION_TASK_SILVER_REQUIRED",
                status=403,
            )
        request_hash = _hash(
            {
                "taskVersionId": task.id,
                "response": response,
                "assistanceUsed": assistance_used,
            }
        )
        replay = self.db.scalar(
            select(CapabilityApplicationSubmission).where(
                CapabilityApplicationSubmission.learning_run_id == run.id,
                CapabilityApplicationSubmission.user_id == self.user_id,
                CapabilityApplicationSubmission.idempotency_key == request_key,
            )
        )
        if replay:
            if replay.request_hash != request_hash:
                raise AppError(
                    "标准应用任务请求标识已用于其他提交",
                    code="IDEMPOTENCY_KEY_REUSED",
                    status=409,
                )
            if replay.status == "completed":
                return _load(replay.result_json, {})
            raise AppError(
                "相同标准应用任务正在评定",
                code="APPLICATION_SUBMISSION_IN_PROGRESS",
                status=409,
            )
        submission = CapabilityApplicationSubmission(
            id=_uid("capability_application_submission"),
            task_version_id=task.id,
            learning_run_id=run.id,
            user_id=self.user_id,
            idempotency_key=request_key,
            request_hash=request_hash,
            response_json=_dump(response),
            assistance_mode=("declared_assisted" if assistance_used else "unassisted_application"),
            status="processing",
        )
        self.db.add(submission)
        self.db.flush()
        rubric = _load(task.rubric_json, [])
        evaluation_candidate = await self.ai.evaluate_standard_application_submission(
            {
                "schemaVersion": "standard_application_evaluation_request_v1",
                "task": {
                    "id": task.id,
                    "prompt": task.prompt,
                    "taskContext": _load(task.task_context_json, {}),
                    "deliverables": _load(task.deliverables_json, []),
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
        evaluator_deployment, evaluator_family, evaluator_model = self._lineage(
            self.ai
        )
        independent = bool(
            task.author_model_family_id
            and evaluator_family
            and task.author_model_family_id != evaluator_family
        )
        passed = (
            evaluation_candidate.verdict == "pass"
            and evaluation_candidate.evidence_sufficiency == "sufficient"
        )
        qualified = bool(
            task.publication_status == "published"
            and task.provenance_mode == "ai_authored"
            and passed
            and not assistance_used
            and independent
        )
        reason = (
            "qualified_unseen_unassisted_independent_evaluation"
            if qualified
            else "declared_assistance_used"
            if assistance_used
            else "task_is_demo_or_not_formally_published"
            if task.publication_status != "published"
            or task.provenance_mode != "ai_authored"
            else "evaluation_not_independent"
            if not independent
            else "evaluation_did_not_pass_with_sufficient_evidence"
        )
        evaluation = CapabilityApplicationEvaluation(
            id=_uid("capability_application_evaluation"),
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
            evaluation_rule_version=APPLICATION_EVALUATION_RULE_VERSION,
        )
        self.db.add(evaluation)
        self.db.flush()
        self._record_observation(
            task=task,
            submission=submission,
            evaluation=evaluation,
            qualified=qualified,
        )
        rebuild_assessment_projections(self.db, user_id=self.user_id)
        state = self.db.scalar(
            select(CapabilityStateProjection).where(
                CapabilityStateProjection.user_id == self.user_id,
                CapabilityStateProjection.capability_revision_id
                == task.capability_revision_id,
            )
        )
        result = {
            "schemaVersion": "standard_application_result_v1",
            "submissionId": submission.id,
            "verdict": evaluation.verdict,
            "evidenceSufficiency": evaluation.evidence_sufficiency,
            "evidenceEligible": qualified,
            "capabilityStage": state.current_stage if state else "unranked",
            "feedback": (
                "已满足全部标准，形成一次正式应用证据。"
                if qualified
                else "本次提交已评定，但没有形成正式黄金能力证据。"
            ),
        }
        submission.status = "completed"
        submission.result_json = _dump(result)
        self.db.commit()
        return result
