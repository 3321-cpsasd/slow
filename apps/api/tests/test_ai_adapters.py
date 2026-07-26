import asyncio
import json

import httpx
from pydantic import BaseModel

from app.ai.anthropic_adapter import AnthropicAdapter
from app.ai.port import ProviderCapabilities


class StructuredAnswer(BaseModel):
    value: str


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
