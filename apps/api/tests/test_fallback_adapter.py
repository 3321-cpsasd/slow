import asyncio

import pytest

from app.ai.fallback_adapter import FallbackAiAdapter
from app.ai.port import ProviderCapabilities
from app.core.errors import AiError


class StubAdapter:
    configured = True

    def __init__(self, model, outcomes):
        self.model = model
        self.outcomes = list(outcomes)
        self.capabilities = ProviderCapabilities(
            protocol="openai",
            api_mode="responses",
            structured_output=True,
            streaming=True,
            reasoning_mode="optional",
        )
        self.input_tokens = 0
        self.output_tokens = 0
        self.closed = False

    async def generate_lesson(self, _spec):
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    def structured_trace(self):
        return [{"schema": "lesson", "outcome": "stub"}]

    def set_usage_recorder(self, _recorder):
        return None

    async def close(self):
        self.closed = True


def test_fallback_adapter_uses_next_model_after_retryable_failure():
    primary = StubAdapter(
        "primary",
        [AiError("暂时不可用", code="AI_PROVIDER_UNAVAILABLE")],
    )
    fallback = StubAdapter("qwen3.8-max-preview", [{"candidate": 1}])
    adapter = FallbackAiAdapter([primary, fallback])

    async def run():
        result = await adapter.generate_lesson({"section": {}})
        return result, adapter.last_model, adapter.fallback_trace()

    result, last_model, trace = asyncio.run(run())

    assert result == {"candidate": 1}
    assert last_model == "qwen3.8-max-preview"
    assert trace == [
        {
            "model": "primary",
            "outcome": "failed",
            "errorCode": "AI_PROVIDER_UNAVAILABLE",
            "retryable": True,
        },
        {"model": "qwen3.8-max-preview", "outcome": "succeeded"},
    ]


def test_fallback_adapter_retries_rejected_complete_candidate():
    primary = StubAdapter("primary", [{"valid": False}])
    fallback = StubAdapter("kimi/kimi-k3", [{"valid": True}])
    adapter = FallbackAiAdapter([primary, fallback])

    async def run():
        result = await adapter.generate_lesson_validated(
            {},
            lambda candidate: (
                None
                if candidate["valid"]
                else (_ for _ in ()).throw(ValueError("contract mismatch"))
            ),
        )
        return result, adapter.last_model, adapter.fallback_trace()

    result, last_model, trace = asyncio.run(run())

    assert result == {"valid": True}
    assert last_model == "kimi/kimi-k3"
    assert [item["outcome"] for item in trace] == [
        "candidate_rejected",
        "succeeded",
    ]


def test_fallback_adapter_does_not_bypass_nonretryable_failure():
    primary = StubAdapter(
        "primary",
        [AiError("请求无效", code="AI_PROVIDER_REJECTED_REQUEST", retryable=False)],
    )
    fallback = StubAdapter("qwen3.8-max-preview", [{"candidate": 1}])
    adapter = FallbackAiAdapter([primary, fallback])

    with pytest.raises(AiError, match="请求无效"):
        asyncio.run(adapter.generate_lesson({}))

    assert fallback.outcomes == [{"candidate": 1}]
