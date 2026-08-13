import asyncio

import pytest

from app.ai.gateway import (
    AiPurpose,
    ModelDeployment,
    ModelDeploymentRegistry,
    PurposeAiGateway,
    RoutePolicy,
    model_family,
)
from app.ai.port import ProviderCapabilities
from app.core.errors import AiError


class StubAdapter:
    configured = True

    def __init__(self, model):
        self.model = model
        self.calls = []
        self.capabilities = ProviderCapabilities(
            protocol="openai",
            api_mode="chat_completions",
            structured_output=True,
            streaming=True,
            reasoning_mode="optional",
        )

    async def generate_lesson(self, spec):
        self.calls.append(("generate_lesson", spec))
        return {"model": self.model}

    async def answer(self, request):
        self.calls.append(("answer", request))
        return {"model": self.model}

    async def answer_stream(self, request):
        self.calls.append(("answer_stream", request))
        yield self.model

    async def ask_me(self, request):
        self.calls.append(("ask_me", request))
        return {"model": self.model}

    def structured_trace(self):
        return []

    def set_usage_recorder(self, _recorder):
        return None

    async def close(self):
        return None

    async def check_connection(self):
        self.calls.append(("check_connection", None))


def gateway(*adapters, ask_ai=None):
    deployments = [
        ModelDeployment(
            deployment_id=f"deployment-{index}",
            provider_id="test",
            model=adapter.model,
            model_family_id=model_family(adapter.model),
            adapter=adapter,
            structured_mode="json_object",
        )
        for index, adapter in enumerate(adapters)
    ]
    deployment_ids = tuple(item.deployment_id for item in deployments)
    policies = {
        purpose.value: RoutePolicy(purpose.value, deployment_ids)
        for purpose in AiPurpose
    }
    if ask_ai:
        ask_deployment = next(
            item for item in deployments if item.adapter is ask_ai
        )
        policies[AiPurpose.ASK_AI.value] = RoutePolicy(
            AiPurpose.ASK_AI.value,
            (ask_deployment.deployment_id,),
        )
    return PurposeAiGateway(
        ModelDeploymentRegistry(deployments, environment="test"),
        policies,
        config_version_id="test-config-v1",
    )


def test_model_family_is_conservative_and_provider_independent():
    assert model_family("qwen3.8-max") == "qwen"
    assert model_family("deepseek-v4-pro") == "deepseek"
    assert model_family("glm-5.2") == "glm"
    assert model_family("vendor/custom") == "vendor"


def test_gateway_routes_ask_ai_without_exposing_model_to_caller():
    author = StubAdapter("qwen3.8-max")
    tutor = StubAdapter("qwen3.6-flash")
    router = gateway(author, tutor, ask_ai=tutor)

    result = asyncio.run(router.answer({"question": "为什么？"}))

    assert result == {"model": "qwen3.6-flash"}
    assert author.calls == []
    assert tutor.calls == [("answer", {"question": "为什么？"})]


def test_gateway_connection_check_covers_each_routed_deployment_once():
    author = StubAdapter("qwen3.8-max")
    evaluator = StubAdapter("glm-5.2")
    router = gateway(author, evaluator)

    asyncio.run(router.check_connection())

    assert author.calls == [("check_connection", None)]
    assert evaluator.calls == [("check_connection", None)]


def test_feedback_resolution_excludes_original_author_family():
    author = StubAdapter("qwen3.8-max")
    same_family = StubAdapter("qwen3.7-plus")
    independent = StubAdapter("glm-5.2")
    router = gateway(author, same_family, independent)
    spec = {"feedback": {
        "feedbackType": "inaccurate",
        "authorModel": "qwen3.8-max",
    }}

    async def run():
        result = await router.generate_lesson(spec)
        return result, router.last_model

    result, selected_model = asyncio.run(run())

    assert result == {"model": "glm-5.2"}
    assert author.calls == []
    assert same_family.calls == []
    assert independent.calls == [("generate_lesson", spec)]
    assert selected_model == "glm-5.2"


def test_feedback_resolution_fails_closed_without_independent_family():
    author = StubAdapter("qwen3.8-max")
    same_family = StubAdapter("qwen3.7-plus")
    router = gateway(author, same_family)

    with pytest.raises(AiError) as captured:
        asyncio.run(router.generate_lesson({
            "feedback": {
                "feedbackType": "inaccurate",
                "authorModel": "qwen3.8-max",
            }
        }))

    assert captured.value.code == "AI_ELIGIBLE_DEPLOYMENT_UNAVAILABLE"
    assert author.calls == []
    assert same_family.calls == []


def test_feedback_style_revision_can_reuse_original_author_family():
    author = StubAdapter("qwen3.8-max")
    router = gateway(author)
    spec = {"feedback": {
        "feedbackType": "unclear",
        "authorModel": "qwen3.8-max",
    }}

    result = asyncio.run(router.generate_lesson(spec))

    assert result == {"model": "qwen3.8-max"}
    assert author.calls == [("generate_lesson", spec)]


def test_independent_route_fails_closed_when_author_lineage_is_missing():
    author = StubAdapter("qwen3.8-max")
    independent = StubAdapter("glm-5.2")
    router = gateway(author, independent)

    with pytest.raises(AiError) as captured:
        asyncio.run(router.ask_me({"previousAnswer": "我的回答"}))

    assert captured.value.code == "AI_AUTHOR_LINEAGE_REQUIRED"
    assert author.calls == []
    assert independent.calls == []


def test_assessment_evaluation_excludes_author_family():
    author = StubAdapter("qwen3.8-max")
    same_family = StubAdapter("qwen3.7-plus")
    evaluator = StubAdapter("glm-5.2")
    router = gateway(author, same_family, evaluator)
    request = {
        "previousAnswer": "我的回答",
        "authorModelFamilyId": "qwen",
    }

    result = asyncio.run(router.ask_me(request))

    assert result == {"model": "glm-5.2"}
    assert author.calls == []
    assert same_family.calls == []
    assert evaluator.calls == [("ask_me", request)]


def test_structured_route_skips_prompt_only_deployment():
    prompt_only = StubAdapter("qwen3.8-max")
    native = StubAdapter("glm-5.2")
    deployments = [
        ModelDeployment(
            "prompt-only", "test", prompt_only.model, "qwen", prompt_only,
            structured_mode="prompt_json",
        ),
        ModelDeployment(
            "native", "test", native.model, "glm", native,
            structured_mode="json_object",
        ),
    ]
    ids = tuple(item.deployment_id for item in deployments)
    policies = {
        purpose.value: RoutePolicy(purpose.value, ids)
        for purpose in AiPurpose
    }
    router = PurposeAiGateway(
        ModelDeploymentRegistry(deployments, environment="test"),
        policies,
        config_version_id="test-config-v1",
    )

    result = asyncio.run(router.answer({"question": "为什么？"}))

    assert result == {"model": "glm-5.2"}
    assert prompt_only.calls == []


def test_route_without_required_structured_capability_is_rejected_at_startup():
    prompt_only = StubAdapter("qwen3.8-max")
    deployment = ModelDeployment(
        "prompt-only", "test", prompt_only.model, "qwen", prompt_only,
        structured_mode="prompt_json",
    )
    policies = {
        purpose.value: RoutePolicy(purpose.value, ("prompt-only",))
        for purpose in AiPurpose
    }

    with pytest.raises(ValueError, match="no structured deployment"):
        PurposeAiGateway(
            ModelDeploymentRegistry([deployment], environment="test"),
            policies,
            config_version_id="test-config-v1",
        )


def test_production_route_rejects_unapproved_backend():
    adapter = StubAdapter("qwen3.8-max")
    deployment = ModelDeployment(
        "unapproved", "test", adapter.model, "qwen", adapter,
        structured_mode="json_object",
        backend_allowed=False,
    )
    policies = {
        purpose.value: RoutePolicy(purpose.value, ("unapproved",))
        for purpose in AiPurpose
    }
    with pytest.raises(ValueError, match="no active deployment"):
        PurposeAiGateway(
            ModelDeploymentRegistry([deployment], environment="production"),
            policies,
            config_version_id="test-config-v1",
        )


def test_unknown_route_deployment_is_rejected_at_startup():
    adapter = StubAdapter("qwen3.8-max")
    deployment = ModelDeployment(
        "author", "test", adapter.model, "qwen", adapter,
        structured_mode="json_object",
    )
    policies = {
        purpose.value: RoutePolicy(purpose.value, ("author",))
        for purpose in AiPurpose
    }
    policies[AiPurpose.ASK_AI.value] = RoutePolicy(
        AiPurpose.ASK_AI.value,
        ("missing",),
    )

    with pytest.raises(ValueError, match="unknown deployment"):
        PurposeAiGateway(
            ModelDeploymentRegistry([deployment], environment="test"),
            policies,
            config_version_id="test-config-v1",
        )


def test_stream_does_not_splice_models_after_first_delta():
    class InterruptedAdapter(StubAdapter):
        async def answer_stream(self, request):
            self.calls.append(("answer_stream", request))
            yield "partial"
            raise AiError("interrupted", code="STREAM_INTERRUPTED", retryable=True)

    first = InterruptedAdapter("qwen3.8-max")
    fallback = StubAdapter("glm-5.2")
    router = gateway(first, fallback)

    async def consume():
        chunks = []
        with pytest.raises(AiError) as captured:
            async for chunk in router.answer_stream({"question": "为什么？"}):
                chunks.append(chunk)
        return chunks, captured.value

    chunks, error = asyncio.run(consume())

    assert chunks == ["partial"]
    assert error.code == "STREAM_INTERRUPTED"
    assert fallback.calls == []


def test_validated_single_model_route_still_runs_harness_validator():
    author = StubAdapter("qwen3.8-max")
    router = gateway(author)
    validated = []

    result = asyncio.run(router.generate_lesson_validated(
        {"feedback": {}},
        lambda candidate: validated.append(candidate),
    ))

    assert result == {"model": "qwen3.8-max"}
    assert validated == [result]
