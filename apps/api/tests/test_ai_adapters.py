import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from pydantic import BaseModel
from sqlalchemy import select

from app.ai.anthropic_adapter import AnthropicAdapter
from app.ai.metering import AiUsageRecorder
from app.ai.openai_adapter import OpenAiAdapter
from app.ai.port import ProviderCapabilities
from app.core.errors import AiError
from app.infrastructure.database import build_database
from app.infrastructure.tables import AiInvocation, AiUsageMeasurement, Base


class StructuredAnswer(BaseModel):
    value: str


class FakeProviderError(RuntimeError):
    def __init__(self, *, status_code=None, code=""):
        super().__init__("provider failure")
        self.status_code = status_code
        self.code = code


class FakeChatCompletions:
    def __init__(self, outputs):
        self.outputs = iter(outputs)
        self.calls = []

    async def create(self, **options):
        self.calls.append(options)
        content = next(self.outputs)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content=content),
                )
            ],
            usage=SimpleNamespace(prompt_tokens=3, completion_tokens=2),
        )


async def chat_adapter(outputs):
    adapter = OpenAiAdapter(
        "test-key",
        "qwen-test",
        "https://workspace.example/compatible-mode/v1",
        capabilities=ProviderCapabilities(
            protocol="openai",
            api_mode="chat_completions",
            structured_output=True,
            streaming=True,
            reasoning_mode="optional",
        ),
    )
    await adapter.close()
    completions = FakeChatCompletions(outputs)
    adapter.client = SimpleNamespace(
        chat=SimpleNamespace(completions=completions)
    )
    return adapter, completions


def test_openai_adapter_maps_invalid_key_to_actionable_error():
    error = OpenAiAdapter._provider_error(
        FakeProviderError(status_code=400, code="InvalidApiKey")
    )

    assert error is not None
    assert error.code == "AI_PROVIDER_AUTH_FAILED"
    assert error.retryable is False
    assert "AI 设置" in str(error)


def test_openai_adapter_does_not_misclassify_local_validation_errors():
    assert OpenAiAdapter._provider_error(ValueError("invalid JSON")) is None


def test_openai_chat_repairs_invalid_schema_with_validation_feedback():
    async def run():
        adapter, completions = await chat_adapter(
            ['{"value":7}', '{"value":"ok"}']
        )
        answer = await adapter._parse(
            StructuredAnswer,
            "Return JSON.",
            {"question": "test"},
            100,
        )
        return answer, completions.calls, adapter.structured_trace()

    answer, calls, trace = asyncio.run(run())

    assert answer.value == "ok"
    assert len(calls) == 2
    assert "JSON 结构修复器" in calls[1]["messages"][0]["content"]
    repair_payload = json.loads(calls[1]["messages"][1]["content"])
    assert repair_payload["invalid_output"] == '{"value":7}'
    assert repair_payload["validation_errors"][0]["path"] == "value"
    assert len(trace) == 1
    assert trace[0]["schema"] == "StructuredAnswer"
    assert trace[0]["attempts"] == 2
    assert trace[0]["repairAttempts"] == 1
    assert trace[0]["outcome"] == "succeeded"
    assert len(trace[0]["invalidOutputDigests"]) == 1
    assert trace[0]["lastValidationIssues"][0]["path"] == "value"


def test_openai_chat_fails_closed_after_repair_budget_is_exhausted():
    async def run():
        adapter, completions = await chat_adapter(
            ['{"value":7}', '{"value":8}', '{"value":9}']
        )
        with pytest.raises(AiError) as raised:
            await adapter._parse(
                StructuredAnswer,
                "Return JSON.",
                {"question": "test"},
                100,
            )
        return raised.value, completions.calls, adapter.structured_trace()

    error, calls, trace = asyncio.run(run())

    assert error.code == "AI_STRUCTURED_OUTPUT_INVALID"
    assert len(calls) == 3
    assert trace[0]["outcome"] == "failed"
    assert trace[0]["attempts"] == 3
    assert trace[0]["repairAttempts"] == 2
    assert len(trace[0]["invalidOutputDigests"]) == 3


def test_structured_repair_records_each_physical_provider_request():
    async def run():
        engine, sessions = build_database("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        recorder = AiUsageRecorder(sessions)
        adapter, _completions = await chat_adapter(
            ['{"value":7}', '{"value":"ok"}']
        )
        adapter.set_usage_recorder(recorder)
        answer = await adapter._parse(
            StructuredAnswer,
            "Return JSON.",
            {"question": "test"},
            100,
        )
        with sessions() as db:
            invocations = db.scalars(
                select(AiInvocation).order_by(AiInvocation.started_at)
            ).all()
            measurements = db.scalars(
                select(AiUsageMeasurement)
            ).all()
        engine.dispose()
        return answer, invocations, measurements

    answer, invocations, measurements = asyncio.run(run())

    assert answer.value == "ok"
    assert len(invocations) == 2
    assert all(item.operation == "structured_call" for item in invocations)
    assert all(item.status == "succeeded" for item in invocations)
    assert all(item.attribution_status == "legacy_unverified" for item in invocations)
    assert all(item.subject_user_id is None for item in invocations)
    assert [item.input_tokens for item in measurements] == [3, 3]
    assert [item.output_tokens for item in measurements] == [2, 2]


def test_missing_usage_is_explicit_instead_of_becoming_zero():
    engine, sessions = build_database("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    recorder = AiUsageRecorder(sessions)

    invocation_id = recorder.start(
        provider="openai",
        api_mode="responses",
        model="test-model",
        operation="connection_check",
        attribution_status="system",
    )
    recorder.succeed(invocation_id, None)

    with sessions() as db:
        invocation = db.get(AiInvocation, invocation_id)
        measurement_count = len(
            db.scalars(select(AiUsageMeasurement)).all()
        )
    engine.dispose()

    assert invocation.status == "succeeded"
    assert invocation.usage_status == "missing"
    assert invocation.attribution_status == "system"
    assert invocation.subject_user_id is None
    assert measurement_count == 0


def test_anthropic_adapter_uses_messages_path_and_validates_schema():
    paths = []

    def handler(request: httpx.Request):
        paths.append(request.url.path)
        body = json.loads(request.content)
        assert body["model"] == "qwen-test"
        return httpx.Response(
            200,
            json={
                "content": [
                    {"type": "text", "text": '{"value":"ok"}'}
                ],
                "usage": {"input_tokens": 3, "output_tokens": 2},
            },
        )

    async def run():
        adapter = AnthropicAdapter(
            "test-key",
            "qwen-test",
            "https://workspace.example/apps/anthropic",
            capabilities=ProviderCapabilities(
                protocol="anthropic",
                api_mode="messages",
                structured_output=True,
                streaming=True,
                reasoning_mode="optional",
            ),
            transport=httpx.MockTransport(handler),
        )
        try:
            answer = await adapter._parse(
                StructuredAnswer,
                "Return JSON.",
                {"question": "test"},
                100,
            )
            assert answer.value == "ok"
            assert adapter.input_tokens == 3
            assert adapter.output_tokens == 2
        finally:
            await adapter.close()

    asyncio.run(run())
    assert paths == ["/apps/anthropic/v1/messages"]


def test_anthropic_adapter_repairs_invalid_schema_before_returning():
    calls = []
    outputs = iter(['{"value":7}', '{"value":"fixed"}'])

    def handler(request: httpx.Request):
        body = json.loads(request.content)
        calls.append(body)
        return httpx.Response(
            200,
            json={
                "content": [{"type": "text", "text": next(outputs)}],
                "usage": {"input_tokens": 3, "output_tokens": 2},
            },
        )

    async def run():
        adapter = AnthropicAdapter(
            "test-key",
            "qwen-test",
            "https://workspace.example/apps/anthropic",
            transport=httpx.MockTransport(handler),
        )
        try:
            answer = await adapter._parse(
                StructuredAnswer,
                "Return JSON.",
                {"question": "test"},
                100,
            )
            return answer, adapter.structured_trace()
        finally:
            await adapter.close()

    answer, trace = asyncio.run(run())

    assert answer.value == "fixed"
    assert len(calls) == 2
    assert "JSON 结构修复器" in calls[1]["system"]
    assert trace[0]["repairAttempts"] == 1


def test_anthropic_adapter_streams_text_deltas():
    def handler(_request: httpx.Request):
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=(
                'event: content_block_delta\n'
                'data: {"delta":{"type":"text_delta","text":"你好"}}\n\n'
                'event: content_block_delta\n'
                'data: {"delta":{"type":"text_delta","text":"，世界"}}\n\n'
            ).encode(),
        )

    async def run():
        adapter = AnthropicAdapter(
            "test-key",
            "qwen-test",
            "https://workspace.example/apps/anthropic",
            transport=httpx.MockTransport(handler),
        )
        try:
            return [
                delta
                async for delta in adapter.answer_stream(
                    {"question": "test"}
                )
            ]
        finally:
            await adapter.close()

    assert asyncio.run(run()) == ["你好", "，世界"]
