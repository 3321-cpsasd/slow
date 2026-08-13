from contextvars import ContextVar
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Callable, Iterable

from ..core.errors import AiError, safe_error_code
from .port import ProviderCapabilities
from .route_context import InvocationRouteContext, invocation_route_context


class AiPurpose(StrEnum):
    DEFAULT = "default"
    CURRICULUM = "curriculum"
    LESSON_AUTHOR = "lesson_author"
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
        return await self._call(
            AiTaskEnvelope(
                AiPurpose.CURRICULUM,
                AuthorityLevel.CANDIDATE_ONLY,
                CapabilityRequirements(structured=True),
            ),
            "chapter",
            request,
            memory,
        )

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
        return len(self._candidates(self._lesson_envelope(spec)))

    async def generate_lesson(self, spec):
        return await self._call(
            self._lesson_envelope(spec),
            "generate_lesson",
            spec,
        )

    async def generate_lesson_validated(self, spec, validator):
        return await self._call(
            self._lesson_envelope(spec),
            "generate_lesson",
            spec,
            candidate_validator=validator,
        )

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
