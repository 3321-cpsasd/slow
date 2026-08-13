from contextvars import ContextVar
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Callable, Iterable

from ..core.errors import AiError, safe_error_code
from .contracts import (
    AskMeDiscussionTurn,
    AskMeTurn,
    GeneratedLessonSlotQuestion,
    GeneratedQuiz,
)
from .openai_adapter import (
    _adjudicate_choice_questions,
    _apply_answerless_question_review,
    _apply_chapter_outline_review,
    _apply_lesson_question_review,
    _combine_lesson_candidate,
    _expand_lesson_slots,
    _lesson_question_payload,
)
from .port import ProviderCapabilities
from .route_context import InvocationRouteContext, invocation_route_context


class AiPurpose(StrEnum):
    DEFAULT = "default"
    CURRICULUM = "curriculum"
    CURRICULUM_REVIEW = "curriculum_review"
    LESSON_AUTHOR = "lesson_author"
    ASSESSMENT_ITEM_AUTHOR = "assessment_item_author"
    ASSESSMENT_ITEM_REVIEW = "assessment_item_review"
    ASSESSMENT_ANSWER_ADJUDICATION = "assessment_answer_adjudication"
    ASK_AI = "ask_ai"
    FEEDBACK_STYLE = "feedback_style"
    FEEDBACK_ACCURACY = "feedback_accuracy"
    ASSESSMENT_PROBE = "assessment_probe"
    ASSESSMENT_EVALUATION = "assessment_evaluation"
    NOTE = "note"
    SOURCE_REPAIR = "source_repair"
    SOURCE_REVIEW = "source_review"
    QUALITY_REVIEW = "quality_review"


class AuthorityLevel(StrEnum):
    EPHEMERAL = "ephemeral"
    CANDIDATE_ONLY = "candidate_only"
    EVIDENCE_CANDIDATE = "evidence_candidate"
    SYSTEM_AUDIT = "system_audit"


@dataclass(frozen=True)
class CapabilityRequirements:
    structured: bool = False
    streaming: bool = False


@dataclass(frozen=True)
class LineageConstraints:
    author_deployment_id: str = ""
    author_model_family_id: str = ""
    author_model: str = ""
    exclude_author_family: bool = False
    excluded_deployment_ids: tuple[str, ...] = ()
    excluded_model_family_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class AiTaskEnvelope:
    purpose: AiPurpose
    authority: AuthorityLevel
    requirements: CapabilityRequirements = field(
        default_factory=CapabilityRequirements
    )
    lineage: LineageConstraints = field(default_factory=LineageConstraints)


@dataclass(frozen=True)
class ModelDeployment:
    deployment_id: str
    provider_id: str
    model: str
    model_family_id: str
    adapter: object
    structured_mode: str = "prompt_json"
    streaming: bool = True
    backend_allowed: bool = True
    allowed_environments: tuple[str, ...] = (
        "development",
        "demo",
        "test",
        "production",
    )
    status: str = "active"


@dataclass(frozen=True)
class RoutePolicy:
    purpose: str
    deployment_ids: tuple[str, ...]
    fail_closed: bool = True
    policy_version: str = "ai_route_v1"


def model_family(model: str) -> str:
    """Compatibility inference for legacy configs; new configs must be explicit."""

    normalized = model.strip().lower()
    aliases = {
        "qwen": ("qwen",),
        "deepseek": ("deepseek",),
        "glm": ("glm",),
        "kimi": ("kimi", "moonshot"),
        "minimax": ("minimax",),
        "mimo": ("mimo",),
        "openai": ("gpt", "o1", "o3", "o4"),
        "anthropic": ("claude",),
        "google": ("gemini",),
    }
    for family, prefixes in aliases.items():
        if normalized.startswith(prefixes):
            return family
    return normalized.split("/", 1)[0].split("-", 1)[0]


def _trusted_quiz_material(request, content, prior_questions=None):
    """Project any frozen lesson material into the shared assessment-role contract."""

    prior = list(prior_questions or [])
    if prior:
        raw_targets = [
            {
                "assessmentTargetId": item.get("assessmentTargetId", ""),
                "objective": item.get("objective", ""),
                "required": bool(item.get("core", False)),
            }
            for item in prior
        ]
    else:
        raw_targets = list(request.get("assessmentTargets") or [])
        if not raw_targets:
            raw_targets = [
                {
                    "assessmentTargetId": request.get("assessmentTargetId", ""),
                    "objective": objective,
                    "required": position == 0,
                }
                for position, objective in enumerate(request.get("objectives") or [])
            ]
    targets = []
    for raw in raw_targets:
        objective = str(
            raw.get("objective")
            or raw.get("objectiveStatement")
            or ""
        ).strip()
        if not objective:
            raise ValueError("trusted assessment target is missing its objective")
        targets.append({
            "assessmentTargetId": str(raw.get("assessmentTargetId") or ""),
            "objective": objective,
            "required": bool(raw.get("required", raw.get("core", False))),
        })
    if not 1 <= len(targets) <= 5:
        raise ValueError("trusted quiz generation requires 1-5 target slots")

    blocks = list(content.blocks)
    payload_blocks = []
    targets_by_slot = {}
    evidence_indexes_by_slot = {}
    for position, target in enumerate(targets, 1):
        slot = f"T{position}"
        indexes = [
            index
            for index, block in enumerate(blocks)
            if target["objective"] in set(block.assessment_objectives)
        ]
        if not indexes and prior:
            indexes = [
                index
                for index in prior[position - 1].get("claim_block_indexes", [])
                if isinstance(index, int) and 0 <= index < len(blocks)
            ]
        if not indexes:
            raise ValueError(
                "trusted assessment target has no explicitly bound frozen evidence"
            )
        evidence_indexes_by_slot[slot] = [] if prior else indexes
        targets_by_slot[slot] = target
        payload_blocks.append({
            "slot": f"{slot}_CORE",
            "heading": " / ".join(
                blocks[index].heading for index in indexes if blocks[index].heading
            ),
            "content": "\n\n".join(blocks[index].content for index in indexes),
        })

    question_count = len(prior) if prior else max(4, len(targets))
    if question_count > 5:
        raise ValueError("trusted assessment question count exceeds five")
    author_payload = {
        "questionCount": question_count,
        "learningContractVersionId": request.get("learningContractVersionId", ""),
        "targets": [
            {
                "slot": slot,
                "objective": target["objective"],
                "required": target["required"],
            }
            for slot, target in targets_by_slot.items()
        ],
        "blocks": payload_blocks,
        "priorQuestions": prior,
    }
    return author_payload, targets_by_slot, evidence_indexes_by_slot


def _trusted_quiz_review_payload(author_payload, questions):
    block_by_slot = {
        block["slot"]: block for block in author_payload["blocks"]
    }
    target_by_slot = {
        target["slot"]: target for target in author_payload["targets"]
    }
    return {
        **author_payload,
        "questions": [
            {
                "itemSlot": f"Q{position}",
                "targetSlot": question.target_slot,
                "objective": target_by_slot[question.target_slot]["objective"],
                "prompt": question.prompt,
                "options": [
                    {"optionId": f"O{index}", "content": option}
                    for index, option in enumerate(question.options, 1)
                ],
                "evidence": block_by_slot[f"{question.target_slot}_CORE"],
            }
            for position, question in enumerate(questions, 1)
        ],
    }


class ModelDeploymentRegistry:
    def __init__(
        self,
        deployments: Iterable[ModelDeployment],
        *,
        environment: str,
    ):
        self.environment = environment
        self._deployments: dict[str, ModelDeployment] = {}
        for deployment in deployments:
            if not deployment.deployment_id:
                raise ValueError("model deployment requires a stable deploymentId")
            if deployment.deployment_id in self._deployments:
                raise ValueError(
                    f"duplicate model deployment: {deployment.deployment_id}"
                )
            if not deployment.model_family_id:
                raise ValueError(
                    f"model deployment {deployment.deployment_id} requires modelFamilyId"
                )
            if deployment.structured_mode not in {
                "native_schema",
                "json_object",
                "prompt_json",
                "unsupported",
            }:
                raise ValueError(
                    f"model deployment {deployment.deployment_id} has invalid structuredMode"
                )
            self._deployments[deployment.deployment_id] = deployment

    def get(self, deployment_id: str) -> ModelDeployment:
        try:
            return self._deployments[deployment_id]
        except KeyError as error:
            raise ValueError(
                f"route references unknown deployment: {deployment_id}"
            ) from error

    def all(self) -> list[ModelDeployment]:
        return list(self._deployments.values())

    def eligible(
        self,
        policy: RoutePolicy,
        envelope: AiTaskEnvelope,
    ) -> list[ModelDeployment]:
        candidates: list[ModelDeployment] = []
        for deployment_id in policy.deployment_ids:
            deployment = self.get(deployment_id)
            if deployment.status != "active":
                continue
            if self.environment not in deployment.allowed_environments:
                continue
            if self.environment == "production" and not deployment.backend_allowed:
                continue
            if (
                envelope.requirements.structured
                and deployment.structured_mode
                not in {"native_schema", "json_object"}
            ):
                continue
            if envelope.requirements.streaming and not deployment.streaming:
                continue
            if deployment.deployment_id in envelope.lineage.excluded_deployment_ids:
                continue
            if deployment.model_family_id in envelope.lineage.excluded_model_family_ids:
                continue
            if envelope.lineage.exclude_author_family:
                author_family = envelope.lineage.author_model_family_id
                if not author_family and envelope.lineage.author_model:
                    author_family = model_family(envelope.lineage.author_model)
                if (
                    envelope.lineage.author_deployment_id
                    and deployment.deployment_id
                    == envelope.lineage.author_deployment_id
                ):
                    continue
                if author_family and deployment.model_family_id == author_family:
                    continue
            candidates.append(deployment)
        return candidates


class PurposeAiGateway:
    """Select model deployments for explicit business tasks.

    Harnesses still own prompts and schemas. The gateway enforces deployment
    eligibility, lineage constraints and fallback without changing validation
    or publication authority.
    """

    staged_lesson_generation = True

    def __init__(
        self,
        registry: ModelDeploymentRegistry,
        policies: dict[str, RoutePolicy],
        *,
        config_version_id: str,
    ):
        self.registry = registry
        self.policies = policies
        self.config_version_id = config_version_id
        for purpose in AiPurpose:
            if purpose.value not in policies:
                raise ValueError(f"missing AI route policy: {purpose.value}")
        for policy in policies.values():
            if not policy.deployment_ids:
                raise ValueError(f"AI route {policy.purpose} has no deployments")
            for deployment_id in policy.deployment_ids:
                registry.get(deployment_id)
            baseline = AiTaskEnvelope(
                purpose=AiPurpose(policy.purpose),
                authority=AuthorityLevel.EPHEMERAL,
            )
            if not registry.eligible(policy, baseline):
                raise ValueError(
                    f"AI route {policy.purpose} has no active deployment "
                    f"allowed in {registry.environment}"
                )
            requirements = []
            if policy.purpose != AiPurpose.DEFAULT.value:
                requirements.append(CapabilityRequirements(structured=True))
            if policy.purpose in {
                AiPurpose.ASK_AI.value,
                AiPurpose.FEEDBACK_STYLE.value,
            }:
                requirements.append(CapabilityRequirements(streaming=True))
            for requirement in requirements:
                capable = AiTaskEnvelope(
                    purpose=AiPurpose(policy.purpose),
                    authority=AuthorityLevel.EPHEMERAL,
                    requirements=requirement,
                )
                if not registry.eligible(policy, capable):
                    capability = (
                        "structured"
                        if requirement.structured
                        else "streaming"
                    )
                    raise ValueError(
                        f"AI route {policy.purpose} has no {capability} "
                        "deployment"
                    )
        self._trace = ContextVar(
            f"ai_gateway_trace_{id(self)}",
            default=(),
        )
        self._structured = ContextVar(
            f"ai_gateway_structured_{id(self)}",
            default=(),
        )
        self._last_deployment = ContextVar(
            f"ai_gateway_last_deployment_{id(self)}",
            default=self._default_deployment(),
        )

    def _default_deployment(self) -> ModelDeployment:
        policy = self.policies[AiPurpose.DEFAULT.value]
        candidates = self.registry.eligible(
            policy,
            AiTaskEnvelope(AiPurpose.DEFAULT, AuthorityLevel.EPHEMERAL),
        )
        if not candidates:
            raise ValueError("default AI route has no eligible deployment")
        return candidates[0]

    @property
    def model(self) -> str:
        return self._default_deployment().model

    @property
    def models(self) -> list[str]:
        return [item.model for item in self.registry.all()]

    @property
    def last_model(self) -> str:
        return self._last_deployment.get().model

    @property
    def last_deployment_id(self) -> str:
        return self._last_deployment.get().deployment_id

    @property
    def last_model_family_id(self) -> str:
        return self._last_deployment.get().model_family_id

    @property
    def capabilities(self):
        deployment = self._default_deployment()
        adapter = deployment.adapter.capabilities
        return ProviderCapabilities(
            protocol=adapter.protocol,
            api_mode=adapter.api_mode,
            structured_output=deployment.structured_mode
            in {"native_schema", "json_object"},
            streaming=deployment.streaming,
            reasoning_mode=adapter.reasoning_mode,
        )

    @property
    def configured(self) -> bool:
        return bool(self._default_deployment().adapter.configured)

    @property
    def input_tokens(self) -> int:
        return sum(
            getattr(item.adapter, "input_tokens", 0)
            for item in self.registry.all()
        )

    @property
    def output_tokens(self) -> int:
        return sum(
            getattr(item.adapter, "output_tokens", 0)
            for item in self.registry.all()
        )

    def route_snapshot(self) -> dict[str, dict]:
        return {
            purpose: {
                "deploymentIds": list(policy.deployment_ids),
                "failClosed": policy.fail_closed,
                "policyVersion": policy.policy_version,
            }
            for purpose, policy in self.policies.items()
        }

    def fallback_trace(self) -> list[dict]:
        return list(self._trace.get())

    def structured_trace(self) -> list[dict]:
        return list(self._structured.get())

    def set_usage_recorder(self, recorder) -> None:
        for deployment in self.registry.all():
            deployment.adapter.set_usage_recorder(recorder)

    async def close(self) -> None:
        seen = set()
        for deployment in self.registry.all():
            if id(deployment.adapter) in seen:
                continue
            seen.add(id(deployment.adapter))
            await deployment.adapter.close()

    async def check_connection(self) -> None:
        checked = set()
        for purpose in AiPurpose:
            envelope = AiTaskEnvelope(purpose, AuthorityLevel.EPHEMERAL)
            for deployment in self.registry.eligible(
                self._policy(purpose), envelope
            ):
                if deployment.deployment_id in checked:
                    continue
                checked.add(deployment.deployment_id)
                await deployment.adapter.check_connection()

    async def check_primary_connection(self) -> None:
        envelope = AiTaskEnvelope(AiPurpose.DEFAULT, AuthorityLevel.EPHEMERAL)
        candidates = self.registry.eligible(
            self._policy(AiPurpose.DEFAULT), envelope
        )
        if not candidates:
            raise AiError(
                "默认用途没有可连接的 AI 部署",
                code="AI_ELIGIBLE_DEPLOYMENT_UNAVAILABLE",
                retryable=False,
            )
        await candidates[0].adapter.check_connection()

    def _policy(self, purpose: AiPurpose) -> RoutePolicy:
        return self.policies[purpose.value]

    def _candidates(self, envelope: AiTaskEnvelope) -> list[ModelDeployment]:
        if (
            envelope.lineage.exclude_author_family
            and not envelope.lineage.author_deployment_id
            and not envelope.lineage.author_model_family_id
            and not envelope.lineage.author_model
        ):
            raise AiError(
                "缺少内容作者血缘，无法执行独立评估",
                code="AI_AUTHOR_LINEAGE_REQUIRED",
                retryable=False,
            )
        policy = self._policy(envelope.purpose)
        candidates = self.registry.eligible(policy, envelope)
        if not candidates:
            raise AiError(
                "当前没有满足任务能力与独立性要求的 AI 部署",
                code="AI_ELIGIBLE_DEPLOYMENT_UNAVAILABLE",
                retryable=False,
            )
        return candidates

    def _invocation_context(
        self,
        envelope: AiTaskEnvelope,
        deployment: ModelDeployment,
        fallback_index: int,
    ) -> InvocationRouteContext:
        return InvocationRouteContext(
            purpose=envelope.purpose.value,
            authority=envelope.authority.value,
            deployment_id=deployment.deployment_id,
            model_family_id=deployment.model_family_id,
            config_version_id=self.config_version_id,
            route_policy_version=self._policy(envelope.purpose).policy_version,
            fallback_index=fallback_index,
        )

    def _attempt_context(self, envelope: AiTaskEnvelope) -> dict:
        return {
            "configVersionId": self.config_version_id,
            "routePolicyVersion": self._policy(
                envelope.purpose
            ).policy_version,
        }

    async def _call(
        self,
        envelope: AiTaskEnvelope,
        method: str,
        *args,
        candidate_validator: Callable | None = None,
    ):
        self._trace.set(())
        self._structured.set(())
        attempts: list[dict] = []
        structured: list[dict] = []
        last_error: AiError | None = None
        last_candidate = None
        candidates = self._candidates(envelope)
        for index, deployment in enumerate(candidates):
            self._last_deployment.set(deployment)
            try:
                with invocation_route_context(
                    self._invocation_context(envelope, deployment, index)
                ):
                    candidate = await getattr(deployment.adapter, method)(*args)
                structured.extend([
                    {
                        **item,
                        "deploymentId": deployment.deployment_id,
                        "modelFamilyId": deployment.model_family_id,
                        "model": deployment.model,
                        "fallbackIndex": index,
                    }
                    for item in deployment.adapter.structured_trace()
                ])
                if candidate_validator is not None:
                    try:
                        candidate_validator(candidate)
                    except Exception as validation_error:
                        last_candidate = candidate
                        attempts.append({
                            **self._attempt_context(envelope),
                            "purpose": envelope.purpose.value,
                            "deploymentId": deployment.deployment_id,
                            "modelFamilyId": deployment.model_family_id,
                            "model": deployment.model,
                            "outcome": "candidate_rejected",
                            "errorCode": safe_error_code(validation_error),
                        })
                        if index + 1 < len(candidates):
                            continue
                        self._trace.set(tuple(attempts))
                        self._structured.set(tuple(structured))
                        return last_candidate
                attempts.append({
                    **self._attempt_context(envelope),
                    "purpose": envelope.purpose.value,
                    "deploymentId": deployment.deployment_id,
                    "modelFamilyId": deployment.model_family_id,
                    "model": deployment.model,
                    "outcome": "succeeded",
                })
                self._trace.set(tuple(attempts))
                self._structured.set(tuple(structured))
                return candidate
            except AiError as error:
                last_error = error
                structured.extend([
                    {
                        **item,
                        "deploymentId": deployment.deployment_id,
                        "modelFamilyId": deployment.model_family_id,
                        "model": deployment.model,
                        "fallbackIndex": index,
                    }
                    for item in deployment.adapter.structured_trace()
                ])
                attempts.append({
                    **self._attempt_context(envelope),
                    "purpose": envelope.purpose.value,
                    "deploymentId": deployment.deployment_id,
                    "modelFamilyId": deployment.model_family_id,
                    "model": deployment.model,
                    "outcome": "failed",
                    "errorCode": error.code,
                    "retryable": error.retryable,
                })
                if not error.retryable or index + 1 >= len(candidates):
                    break
            except BaseException as error:
                attempts.append({
                    **self._attempt_context(envelope),
                    "purpose": envelope.purpose.value,
                    "deploymentId": deployment.deployment_id,
                    "modelFamilyId": deployment.model_family_id,
                    "model": deployment.model,
                    "outcome": "failed",
                    "errorCode": safe_error_code(error),
                    "retryable": False,
                })
                self._trace.set(tuple(attempts))
                self._structured.set(tuple(structured))
                raise
        self._trace.set(tuple(attempts))
        self._structured.set(tuple(structured))
        if last_error:
            if len(attempts) > 1:
                raise AiError(
                    str(last_error),
                    code=last_error.code,
                    retryable=False,
                    operation_id=last_error.operation_id,
                ) from last_error
            raise last_error
        raise AiError(
            "模型路由未返回候选结果",
            code="AI_ROUTE_EXHAUSTED",
            retryable=False,
        )

    async def _stream(self, envelope: AiTaskEnvelope, method: str, request: dict):
        self._trace.set(())
        self._structured.set(())
        attempts: list[dict] = []
        candidates = self._candidates(envelope)
        for index, deployment in enumerate(candidates):
            self._last_deployment.set(deployment)
            produced = False
            try:
                with invocation_route_context(
                    self._invocation_context(envelope, deployment, index)
                ):
                    async for delta in getattr(deployment.adapter, method)(request):
                        if delta:
                            produced = True
                            yield delta
                attempts.append({
                    **self._attempt_context(envelope),
                    "purpose": envelope.purpose.value,
                    "deploymentId": deployment.deployment_id,
                    "modelFamilyId": deployment.model_family_id,
                    "model": deployment.model,
                    "outcome": "succeeded",
                })
                self._trace.set(tuple(attempts))
                return
            except AiError as error:
                attempts.append({
                    **self._attempt_context(envelope),
                    "purpose": envelope.purpose.value,
                    "deploymentId": deployment.deployment_id,
                    "modelFamilyId": deployment.model_family_id,
                    "model": deployment.model,
                    "outcome": "interrupted" if produced else "failed",
                    "errorCode": error.code,
                    "retryable": error.retryable and not produced,
                })
                if produced or not error.retryable or index + 1 >= len(candidates):
                    self._trace.set(tuple(attempts))
                    raise
            except BaseException as error:
                attempts.append({
                    **self._attempt_context(envelope),
                    "purpose": envelope.purpose.value,
                    "deploymentId": deployment.deployment_id,
                    "modelFamilyId": deployment.model_family_id,
                    "model": deployment.model,
                    "outcome": "interrupted" if produced else "failed",
                    "errorCode": safe_error_code(error),
                    "retryable": False,
                })
                self._trace.set(tuple(attempts))
                raise
        self._trace.set(tuple(attempts))
        raise AiError("流式模型路由已耗尽", code="AI_ROUTE_EXHAUSTED")

    @staticmethod
    def _lineage(payload: dict, *, exclude: bool) -> LineageConstraints:
        return LineageConstraints(
            author_deployment_id=str(payload.get("authorDeploymentId") or ""),
            author_model_family_id=str(payload.get("authorModelFamilyId") or ""),
            author_model=str(payload.get("authorModel") or ""),
            exclude_author_family=exclude,
        )

    @staticmethod
    def _ask_me_evaluation_lineage(payload: dict) -> LineageConstraints:
        probe_deployment = str(payload.get("probeDeploymentId") or "")
        probe_family = str(payload.get("probeModelFamilyId") or "")
        return LineageConstraints(
            author_deployment_id=str(payload.get("authorDeploymentId") or ""),
            author_model_family_id=str(payload.get("authorModelFamilyId") or ""),
            author_model=str(payload.get("authorModel") or ""),
            exclude_author_family=True,
            excluded_deployment_ids=(probe_deployment,) if probe_deployment else (),
            excluded_model_family_ids=(probe_family,) if probe_family else (),
        )

    @staticmethod
    def _ask_me_probe_lineage(
        payload: dict,
        evaluator: ModelDeployment,
        unsupported: tuple[ModelDeployment, ...] = (),
    ) -> LineageConstraints:
        deployment_ids = [
            evaluator.deployment_id,
            str(payload.get("authorDeploymentId") or ""),
            *(item.deployment_id for item in unsupported),
        ]
        family_ids = [
            evaluator.model_family_id,
            str(payload.get("authorModelFamilyId") or ""),
            *(item.model_family_id for item in unsupported),
        ]
        return LineageConstraints(
            excluded_deployment_ids=tuple(
                dict.fromkeys(item for item in deployment_ids if item)
            ),
            excluded_model_family_ids=tuple(
                dict.fromkeys(item for item in family_ids if item)
            ),
        )

    @staticmethod
    def _extend_lineage(
        lineage: LineageConstraints,
        deployments: tuple[ModelDeployment, ...],
    ) -> LineageConstraints:
        return LineageConstraints(
            author_deployment_id=lineage.author_deployment_id,
            author_model_family_id=lineage.author_model_family_id,
            author_model=lineage.author_model,
            exclude_author_family=lineage.exclude_author_family,
            excluded_deployment_ids=tuple(dict.fromkeys((
                *lineage.excluded_deployment_ids,
                *(item.deployment_id for item in deployments),
            ))),
            excluded_model_family_ids=tuple(dict.fromkeys((
                *lineage.excluded_model_family_ids,
                *(item.model_family_id for item in deployments),
            ))),
        )

    async def plan(self, request, memory):
        return await self._call(
            AiTaskEnvelope(
                AiPurpose.CURRICULUM,
                AuthorityLevel.CANDIDATE_ONLY,
                CapabilityRequirements(structured=True),
            ),
            "plan",
            request,
            memory,
        )

    async def chapter(self, request, memory):
        envelope = AiTaskEnvelope(
            AiPurpose.CURRICULUM,
            AuthorityLevel.CANDIDATE_ONLY,
            CapabilityRequirements(structured=True),
        )
        authored = await self._call(
            envelope,
            "chapter",
            request,
            memory,
        )
        author = self._last_deployment.get()
        trace = list(self._trace.get())
        structured = list(self._structured.get())
        review_payload = {
            "chapter": request,
            "candidateSections": [
                {
                    "sectionSlot": f"S{position}",
                    **section.model_dump(by_alias=True),
                }
                for position, section in enumerate(authored.sections, 1)
            ],
        }
        try:
            unsupported = tuple(
                deployment
                for deployment in self.registry.all()
                if not hasattr(deployment.adapter, "review_chapter_outline")
            )
            review = await self._call(
                AiTaskEnvelope(
                    AiPurpose.CURRICULUM_REVIEW,
                    AuthorityLevel.SYSTEM_AUDIT,
                    CapabilityRequirements(structured=True),
                    self._exclude_deployments(author, *unsupported),
                ),
                "review_chapter_outline",
                review_payload,
            )
            trace.extend(self._trace.get())
            structured.extend(self._structured.get())
            result = _apply_chapter_outline_review(authored, review)
            trace.append({
                "purpose": "chapter_outline_scope_review",
                "outcome": "succeeded",
                "authorDeploymentId": author.deployment_id,
                "reviewerDeploymentId": self._last_deployment.get().deployment_id,
                "editCount": sum(
                    item.decision == "edit" for item in review.sections
                ),
            })
        except Exception as error:
            trace.extend(self._trace.get())
            structured.extend(self._structured.get())
            trace.append({
                "purpose": "chapter_outline_scope_review",
                "outcome": "skipped",
                "authorDeploymentId": author.deployment_id,
                "errorCode": (
                    error.code if isinstance(error, AiError) else safe_error_code(error)
                ),
            })
            if self.registry.environment == "production":
                self._trace.set(tuple(trace))
                self._structured.set(tuple(structured))
                raise AiError(
                    "章节范围审校暂未完成；目录不会以未审状态发布",
                    code="AI_CURRICULUM_REVIEW_REQUIRED",
                    retryable=True,
                ) from error
            result = authored
        self._trace.set(tuple(trace))
        self._structured.set(tuple(structured))
        return result

    async def replan_book(self, request, memory):
        return await self._call(
            AiTaskEnvelope(
                AiPurpose.CURRICULUM,
                AuthorityLevel.CANDIDATE_ONLY,
                CapabilityRequirements(structured=True),
            ),
            "replan_book",
            request,
            memory,
        )

    async def teaching_blueprint(self, request, memory):
        return await self._call(
            AiTaskEnvelope(
                AiPurpose.LESSON_AUTHOR,
                AuthorityLevel.CANDIDATE_ONLY,
                CapabilityRequirements(structured=True),
            ),
            "teaching_blueprint",
            request,
            memory,
        )

    def _lesson_envelope(self, spec: dict) -> AiTaskEnvelope:
        feedback = spec.get("feedback") or {}
        if not feedback:
            purpose = AiPurpose.LESSON_AUTHOR
            lineage = LineageConstraints()
        elif feedback.get("feedbackType") == "inaccurate":
            purpose = AiPurpose.FEEDBACK_ACCURACY
            lineage = self._lineage(feedback, exclude=True)
        else:
            purpose = AiPurpose.FEEDBACK_STYLE
            lineage = self._lineage(feedback, exclude=False)
        return AiTaskEnvelope(
            purpose,
            AuthorityLevel.CANDIDATE_ONLY,
            CapabilityRequirements(structured=True),
            lineage,
        )

    def lesson_call_budget(self, spec: dict) -> int:
        purposes = (
            self._lesson_envelope(spec).purpose,
            AiPurpose.ASSESSMENT_ITEM_AUTHOR,
            AiPurpose.ASSESSMENT_ITEM_REVIEW,
            AiPurpose.ASSESSMENT_ANSWER_ADJUDICATION,
        )
        return sum(len(self._policy(purpose).deployment_ids) for purpose in purposes)

    @staticmethod
    def _exclude_deployments(*deployments: ModelDeployment) -> LineageConstraints:
        return LineageConstraints(
            excluded_deployment_ids=tuple(
                dict.fromkeys(item.deployment_id for item in deployments)
            ),
            excluded_model_family_ids=tuple(
                dict.fromkeys(item.model_family_id for item in deployments)
            ),
        )

    def _unsupported_deployments(self, method: str) -> tuple[ModelDeployment, ...]:
        return tuple(
            deployment
            for deployment in self.registry.all()
            if not hasattr(deployment.adapter, method)
        )

    def _purpose_supports(self, purpose: AiPurpose, method: str) -> bool:
        envelope = AiTaskEnvelope(
            purpose,
            AuthorityLevel.CANDIDATE_ONLY,
            CapabilityRequirements(structured=True),
        )
        return any(
            hasattr(deployment.adapter, method)
            for deployment in self.registry.eligible(
                self._policy(purpose),
                envelope,
            )
        )

    async def _trusted_lesson_pipeline(self, spec, validator=None):
        trace: list[dict] = []
        structured: list[dict] = []

        def collect() -> None:
            trace.extend(self._trace.get())
            structured.extend(self._structured.get())

        content = await self._call(
            self._lesson_envelope(spec),
            "author_lesson_content",
            spec,
        )
        content_deployment = self._last_deployment.get()
        collect()
        if content.decision == "replan_required":
            result = _expand_lesson_slots(
                _combine_lesson_candidate(content, None),
                spec,
            )
            if validator is not None:
                validator(result)
            self._trace.set(tuple(trace))
            self._structured.set(tuple(structured))
            return result

        authored = await self._call(
            AiTaskEnvelope(
                AiPurpose.ASSESSMENT_ITEM_AUTHOR,
                AuthorityLevel.CANDIDATE_ONLY,
                CapabilityRequirements(structured=True),
            ),
            "author_lesson_questions",
            _lesson_question_payload(content, spec),
        )
        item_author = self._last_deployment.get()
        collect()
        slot_candidate = _combine_lesson_candidate(content, authored)

        review = await self._call(
            AiTaskEnvelope(
                AiPurpose.ASSESSMENT_ITEM_REVIEW,
                AuthorityLevel.SYSTEM_AUDIT,
                CapabilityRequirements(structured=True),
                self._exclude_deployments(item_author),
            ),
            "review_lesson_questions",
            _lesson_question_payload(content, spec, slot_candidate.questions),
        )
        reviewer = self._last_deployment.get()
        collect()
        try:
            reviewed = _apply_lesson_question_review(slot_candidate, review)
        except ValueError as error:
            raise AiError(
                "独立审题未形成可发布题集；本次候选已失败",
                code="AI_ASSESSMENT_REVIEW_REJECTED",
                retryable=False,
            ) from error

        adjudication = await self._call(
            AiTaskEnvelope(
                AiPurpose.ASSESSMENT_ANSWER_ADJUDICATION,
                AuthorityLevel.EVIDENCE_CANDIDATE,
                CapabilityRequirements(structured=True),
                self._exclude_deployments(item_author, reviewer),
            ),
            "adjudicate_lesson_questions",
            _lesson_question_payload(content, spec, reviewed.questions),
        )
        collect()
        try:
            result = _expand_lesson_slots(reviewed, spec, adjudication)
            if validator is not None:
                validator(result)
        except Exception as error:
            raise AiError(
                "答案盲判未形成唯一可发布答案；本次候选已失败",
                code="AI_ASSESSMENT_ADJUDICATION_REJECTED",
                retryable=False,
            ) from error
        trace.append({
            "purpose": "trusted_assessment_pipeline",
            "outcome": "succeeded",
            "contentDeploymentId": content_deployment.deployment_id,
            "itemAuthorDeploymentId": item_author.deployment_id,
            "reviewerDeploymentId": reviewer.deployment_id,
            "adjudicatorDeploymentId": self._last_deployment.get().deployment_id,
            "reviewDecisionCounts": {
                decision: sum(
                    item.decision == decision for item in review.questions
                )
                for decision in ("accept", "edit", "reject")
            },
        })
        self._trace.set(tuple(trace))
        self._structured.set(tuple(structured))
        return result

    def _supports_trusted_assessment_pipeline(self) -> bool:
        return all((
            self._purpose_supports(
                AiPurpose.LESSON_AUTHOR,
                "author_lesson_content",
            ),
            self._purpose_supports(
                AiPurpose.ASSESSMENT_ITEM_AUTHOR,
                "author_lesson_questions",
            ),
            self._purpose_supports(
                AiPurpose.ASSESSMENT_ITEM_REVIEW,
                "review_lesson_questions",
            ),
            self._purpose_supports(
                AiPurpose.ASSESSMENT_ANSWER_ADJUDICATION,
                "adjudicate_lesson_questions",
            ),
        ))

    def _supports_trusted_quiz_pipeline(self) -> bool:
        return all((
            self._purpose_supports(
                AiPurpose.ASSESSMENT_ITEM_AUTHOR,
                "author_lesson_questions",
            ),
            self._purpose_supports(
                AiPurpose.ASSESSMENT_ITEM_REVIEW,
                "review_lesson_questions",
            ),
            self._purpose_supports(
                AiPurpose.ASSESSMENT_ANSWER_ADJUDICATION,
                "adjudicate_lesson_questions",
            ),
        ))

    async def _trusted_quiz_pipeline(
        self,
        request,
        content,
        prior_questions=None,
    ) -> GeneratedQuiz:
        trace: list[dict] = []
        structured: list[dict] = []

        def collect() -> None:
            trace.extend(self._trace.get())
            structured.extend(self._structured.get())

        try:
            material, targets, evidence_indexes = _trusted_quiz_material(
                request,
                content,
                prior_questions,
            )
            authored = await self._call(
                AiTaskEnvelope(
                    AiPurpose.ASSESSMENT_ITEM_AUTHOR,
                    AuthorityLevel.CANDIDATE_ONLY,
                    CapabilityRequirements(structured=True),
                    self._exclude_deployments(
                        *self._unsupported_deployments(
                            "author_lesson_questions"
                        )
                    ),
                ),
                "author_lesson_questions",
                material,
            )
            item_author = self._last_deployment.get()
            collect()
            if len(authored.questions) != material["questionCount"]:
                raise ValueError("item author returned the wrong question count")
            if prior_questions and any(
                question.target_slot != f"T{position}"
                for position, question in enumerate(authored.questions, 1)
            ):
                raise ValueError("replacement questions changed their target positions")
            required_slots = {
                slot for slot, target in targets.items() if target["required"]
            }
            if not required_slots.issubset({
                question.target_slot for question in authored.questions
            }):
                raise ValueError("authored questions missed a required target")

            review = await self._call(
                AiTaskEnvelope(
                    AiPurpose.ASSESSMENT_ITEM_REVIEW,
                    AuthorityLevel.SYSTEM_AUDIT,
                    CapabilityRequirements(structured=True),
                    self._exclude_deployments(
                        item_author,
                        *self._unsupported_deployments(
                            "review_lesson_questions"
                        ),
                    ),
                ),
                "review_lesson_questions",
                _trusted_quiz_review_payload(material, authored.questions),
            )
            reviewer = self._last_deployment.get()
            collect()
            reviewed = _apply_answerless_question_review(
                [
                    GeneratedLessonSlotQuestion.model_validate(
                        question.model_dump()
                    )
                    for question in authored.questions
                ],
                review,
            )

            adjudication = await self._call(
                AiTaskEnvelope(
                    AiPurpose.ASSESSMENT_ANSWER_ADJUDICATION,
                    AuthorityLevel.EVIDENCE_CANDIDATE,
                    CapabilityRequirements(structured=True),
                    self._exclude_deployments(
                        item_author,
                        reviewer,
                        *self._unsupported_deployments(
                            "adjudicate_lesson_questions"
                        ),
                    ),
                ),
                "adjudicate_lesson_questions",
                _trusted_quiz_review_payload(material, reviewed),
            )
            collect()
            result = _adjudicate_choice_questions(
                reviewed,
                adjudication,
                targets_by_slot=targets,
                evidence_indexes_by_slot=evidence_indexes,
                seed=str(
                    request.get("learningContractVersionId")
                    or request.get("reviewAssignmentId")
                    or request.get("reinforcementRunId")
                    or request.get("id")
                    or "assessment"
                ),
            )
        except AiError:
            self._trace.set(tuple(trace + list(self._trace.get())))
            self._structured.set(tuple(structured + list(self._structured.get())))
            raise
        except Exception as error:
            self._trace.set(tuple(trace + list(self._trace.get())))
            self._structured.set(tuple(structured + list(self._structured.get())))
            raise AiError(
                "可信测评未形成可发布题目；本次候选已失败",
                code="AI_TRUSTED_ASSESSMENT_REJECTED",
                retryable=False,
            ) from error
        trace.append({
            "purpose": "trusted_assessment_quiz_pipeline",
            "outcome": "succeeded",
            "itemAuthorDeploymentId": item_author.deployment_id,
            "reviewerDeploymentId": reviewer.deployment_id,
            "adjudicatorDeploymentId": self._last_deployment.get().deployment_id,
            "questionCount": len(result.questions),
        })
        self._trace.set(tuple(trace))
        self._structured.set(tuple(structured))
        return result

    async def generate_lesson(self, spec):
        if not self._supports_trusted_assessment_pipeline():
            return await self._call(
                self._lesson_envelope(spec),
                "generate_lesson",
                spec,
            )
        return await self._trusted_lesson_pipeline(spec)

    async def generate_lesson_validated(self, spec, validator):
        if not self._supports_trusted_assessment_pipeline():
            return await self._call(
                self._lesson_envelope(spec),
                "generate_lesson",
                spec,
                candidate_validator=validator,
            )
        return await self._trusted_lesson_pipeline(spec, validator)

    async def lesson(self, request, memory, prior_questions=None):
        return await self._call(
            AiTaskEnvelope(
                AiPurpose.LESSON_AUTHOR,
                AuthorityLevel.CANDIDATE_ONLY,
                CapabilityRequirements(structured=True),
            ),
            "lesson",
            request,
            memory,
            prior_questions,
        )

    async def lesson_content(self, request, memory, prior_questions=None):
        return await self._call(
            AiTaskEnvelope(
                AiPurpose.LESSON_AUTHOR,
                AuthorityLevel.CANDIDATE_ONLY,
                CapabilityRequirements(structured=True),
            ),
            "lesson_content",
            request,
            memory,
            prior_questions,
        )

    async def lesson_quiz(self, request, content, prior_questions=None):
        if self._supports_trusted_quiz_pipeline():
            return await self._trusted_quiz_pipeline(
                request,
                content,
                prior_questions,
            )
        return await self._call(
            AiTaskEnvelope(
                AiPurpose.LESSON_AUTHOR,
                AuthorityLevel.CANDIDATE_ONLY,
                CapabilityRequirements(structured=True),
            ),
            "lesson_quiz",
            request,
            content,
            prior_questions,
        )

    async def repair_lesson_sources(
        self,
        request,
        memory,
        content,
        failed_sources,
        prior_questions=None,
    ):
        return await self._call(
            AiTaskEnvelope(
                AiPurpose.SOURCE_REPAIR,
                AuthorityLevel.CANDIDATE_ONLY,
                CapabilityRequirements(structured=True),
            ),
            "repair_lesson_sources",
            request,
            memory,
            content,
            failed_sources,
            prior_questions,
        )

    async def review_lesson_alignment(self, request, content, quiz):
        return await self._call(
            AiTaskEnvelope(
                AiPurpose.QUALITY_REVIEW,
                AuthorityLevel.SYSTEM_AUDIT,
                CapabilityRequirements(structured=True),
            ),
            "review_lesson_alignment",
            request,
            content,
            quiz,
        )

    async def review_source_claim(self, request):
        return await self._call(
            AiTaskEnvelope(
                AiPurpose.SOURCE_REVIEW,
                AuthorityLevel.SYSTEM_AUDIT,
                CapabilityRequirements(structured=True),
            ),
            "review_source_claim",
            request,
        )

    async def answer(self, request):
        return await self._call(
            AiTaskEnvelope(
                AiPurpose.ASK_AI,
                AuthorityLevel.EPHEMERAL,
                CapabilityRequirements(structured=True),
            ),
            "answer",
            request,
        )

    async def answer_stream(self, request):
        envelope = AiTaskEnvelope(
            AiPurpose.ASK_AI,
            AuthorityLevel.EPHEMERAL,
            CapabilityRequirements(streaming=True),
        )
        async for delta in self._stream(envelope, "answer_stream", request):
            yield delta

    async def repair_stream(self, request):
        envelope = AiTaskEnvelope(
            AiPurpose.FEEDBACK_STYLE,
            AuthorityLevel.CANDIDATE_ONLY,
            CapabilityRequirements(streaming=True),
        )
        async for delta in self._stream(envelope, "repair_stream", request):
            yield delta

    async def note(self, request):
        return await self._call(
            AiTaskEnvelope(
                AiPurpose.NOTE,
                AuthorityLevel.CANDIDATE_ONLY,
                CapabilityRequirements(structured=True),
            ),
            "note",
            request,
        )

    async def ask_me(self, request):
        evaluates_answer = bool(request.get("previousAnswer"))
        split_supported = (
            self._purpose_supports(AiPurpose.ASSESSMENT_PROBE, "ask_me_probe")
            and self._purpose_supports(
                AiPurpose.ASSESSMENT_EVALUATION,
                "evaluate_ask_me",
            )
        )
        if split_supported and not evaluates_answer:
            probe = await self._call(
                AiTaskEnvelope(
                    AiPurpose.ASSESSMENT_PROBE,
                    AuthorityLevel.EPHEMERAL,
                    CapabilityRequirements(structured=True),
                    self._exclude_deployments(
                        *self._unsupported_deployments("ask_me_probe")
                    ),
                ),
                "ask_me_probe",
                request,
            )
            return AskMeTurn(
                dimension=probe.dimension,
                prompt=probe.prompt,
                evaluation="not_evaluated",
            )
        if split_supported:
            trace: list[dict] = []
            structured: list[dict] = []
            evaluation_lineage = self._extend_lineage(
                self._ask_me_evaluation_lineage(request),
                self._unsupported_deployments("evaluate_ask_me"),
            )
            evaluation = await self._call(
                AiTaskEnvelope(
                    AiPurpose.ASSESSMENT_EVALUATION,
                    AuthorityLevel.EVIDENCE_CANDIDATE,
                    CapabilityRequirements(structured=True),
                    evaluation_lineage,
                ),
                "evaluate_ask_me",
                request,
            )
            evaluator = self._last_deployment.get()
            trace.extend(self._trace.get())
            structured.extend(self._structured.get())
            prompt = ""
            if not request.get("finalize"):
                probe = await self._call(
                    AiTaskEnvelope(
                        AiPurpose.ASSESSMENT_PROBE,
                        AuthorityLevel.EPHEMERAL,
                        CapabilityRequirements(structured=True),
                        self._ask_me_probe_lineage(
                            request,
                            evaluator,
                            self._unsupported_deployments("ask_me_probe"),
                        ),
                    ),
                    "ask_me_probe",
                    request,
                )
                trace.extend(self._trace.get())
                structured.extend(self._structured.get())
                prompt = probe.prompt
                if probe.dimension != request["dimension"]:
                    raise AiError(
                        "Ask Me 探测模型改变了服务端指定维度",
                        code="ASK_ME_PROBE_DIMENSION_INVALID",
                        retryable=False,
                    )
            trace.append({
                "purpose": "ask_me_role_separation",
                "outcome": "succeeded",
                "evaluatorDeploymentId": evaluator.deployment_id,
                "probeDeploymentId": (
                    self._last_deployment.get().deployment_id if prompt else ""
                ),
            })
            self._trace.set(tuple(trace))
            self._structured.set(tuple(structured))
            return AskMeTurn(
                dimension=request["dimension"],
                prompt=prompt,
                evaluation=evaluation.evaluation,
                rationale=evaluation.rationale,
            )
        purpose = (
            AiPurpose.ASSESSMENT_EVALUATION
            if evaluates_answer
            else AiPurpose.ASSESSMENT_PROBE
        )
        return await self._call(
            AiTaskEnvelope(
                purpose,
                (
                    AuthorityLevel.EVIDENCE_CANDIDATE
                    if evaluates_answer
                    else AuthorityLevel.EPHEMERAL
                ),
                CapabilityRequirements(structured=True),
                self._lineage(request, exclude=evaluates_answer),
            ),
            "ask_me",
            request,
        )

    async def ask_me_discussion(self, request):
        split_supported = (
            self._purpose_supports(
                AiPurpose.ASSESSMENT_EVALUATION,
                "evaluate_ask_me_discussion",
            )
            and self._purpose_supports(
                AiPurpose.ASSESSMENT_PROBE,
                "ask_me_discussion_probe",
            )
        )
        if split_supported:
            evaluation = await self._call(
                AiTaskEnvelope(
                    AiPurpose.ASSESSMENT_EVALUATION,
                    AuthorityLevel.EVIDENCE_CANDIDATE,
                    CapabilityRequirements(structured=True),
                    self._extend_lineage(
                        self._ask_me_evaluation_lineage(request),
                        self._unsupported_deployments(
                            "evaluate_ask_me_discussion"
                        ),
                    ),
                ),
                "evaluate_ask_me_discussion",
                request,
            )
            evaluator = self._last_deployment.get()
            trace = list(self._trace.get())
            structured = list(self._structured.get())
            probe = await self._call(
                AiTaskEnvelope(
                    AiPurpose.ASSESSMENT_PROBE,
                    AuthorityLevel.EPHEMERAL,
                    CapabilityRequirements(structured=True),
                    self._ask_me_probe_lineage(
                        request,
                        evaluator,
                        self._unsupported_deployments(
                            "ask_me_discussion_probe"
                        ),
                    ),
                ),
                "ask_me_discussion_probe",
                request,
            )
            trace.extend(self._trace.get())
            structured.extend(self._structured.get())
            trace.append({
                "purpose": "ask_me_discussion_role_separation",
                "outcome": "succeeded",
                "evaluatorDeploymentId": evaluator.deployment_id,
                "probeDeploymentId": self._last_deployment.get().deployment_id,
            })
            self._trace.set(tuple(trace))
            self._structured.set(tuple(structured))
            return AskMeDiscussionTurn(
                evaluation=evaluation.evaluation,
                correct_points=evaluation.correct_points,
                issues=evaluation.issues,
                suggestions=evaluation.suggestions,
                follow_up_prompt=probe.follow_up_prompt,
                follow_up_purpose=probe.follow_up_purpose,
                topic_sufficiency=evaluation.topic_sufficiency,
            )
        return await self._call(
            AiTaskEnvelope(
                AiPurpose.ASSESSMENT_EVALUATION,
                AuthorityLevel.EVIDENCE_CANDIDATE,
                CapabilityRequirements(structured=True),
                self._lineage(request, exclude=True),
            ),
            "ask_me_discussion",
            request,
        )
