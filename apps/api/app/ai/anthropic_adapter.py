import json

import httpx
from pydantic import ValidationError

from ..core.errors import AiError
from .openai_adapter import OpenAiAdapter
from .port import ProviderCapabilities


class AnthropicAdapter(OpenAiAdapter):
    """Anthropic Messages transport with the same validated business contract."""

    def __init__(
        self,
        api_key: str,
        model: str,
        base_url: str = "",
        capabilities: ProviderCapabilities | None = None,
        *,
        transport=None,
    ):
        self.model = model
        self.capabilities = capabilities or ProviderCapabilities(
            protocol="anthropic",
            api_mode="messages",
            structured_output=True,
            streaming=True,
            reasoning_mode="optional",
        )
        endpoint = (base_url or "https://api.anthropic.com").rstrip("/") + "/"
        self.client = (
            httpx.AsyncClient(
                base_url=endpoint,
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                timeout=120,
                transport=transport,
            )
            if api_key
            else None
        )
        self.input_tokens = 0
        self.output_tokens = 0

    @property
    def configured(self):
        return self.client is not None

    async def close(self):
        if self.client:
            await self.client.aclose()

    async def check_connection(self):
        await self._message(
            system="Reply with only OK.",
            user="Connection check.",
            max_tokens=16,
        )

    async def _message(
        self,
        *,
        system: str,
        user: str,
        max_tokens: int,
        stream: bool = False,
    ):
        if not self.client:
            raise AiError("未配置 Anthropic API Key")
        body = {
            "model": self.model,
            "system": system,
            "messages": [{"role": "user", "content": user}],
            "max_tokens": max_tokens,
            "stream": stream,
        }
        if (
            self.capabilities.reasoning_mode == "required"
            and max_tokens > 600
        ):
            body["thinking"] = {
                "type": "enabled",
                "budget_tokens": 600,
            }
        try:
            response = await self.client.post("v1/messages", json=body)
            response.raise_for_status()
            payload = response.json()
            self._record_anthropic_usage(payload.get("usage"))
            return payload
        except httpx.HTTPStatusError as error:
            status = error.response.status_code
            raise AiError(
                "AI 服务拒绝了当前请求，请检查协议、地址、模型和密钥",
                code="AI_PROVIDER_REJECTED_REQUEST",
                retryable=status == 429 or status >= 500,
            ) from error
        except httpx.HTTPError as error:
            raise AiError(
                "无法连接 AI 服务，请稍后重试",
                code="AI_PROVIDER_UNAVAILABLE",
                retryable=True,
            ) from error

    async def _parse(self, schema, developer: str, payload: dict, tokens: int):
        schema_text = json.dumps(
            schema.model_json_schema(),
            ensure_ascii=False,
        )
        error = None
        for attempt in range(3):
            try:
                response = await self._message(
                    system=(
                        f"{developer}\n"
                        "只输出一个符合以下 JSON Schema 的 JSON 对象，"
                        "不要使用 Markdown：\n"
                        f"{schema_text}"
                    ),
                    user=json.dumps(payload, ensure_ascii=False),
                    max_tokens=tokens,
                )
                content = "".join(
                    block.get("text", "")
                    for block in response.get("content", [])
                    if block.get("type") == "text"
                ).strip()
                if content.startswith("```"):
                    content = content.split("\n", 1)[-1].rsplit("```", 1)[0]
                return schema.model_validate_json(content.strip())
            except ValidationError as validation_error:
                error = validation_error
                if attempt == 2:
                    break
            except AiError:
                raise
        raise AiError(
            "AI 未返回有效的结构化结果，请稍后重试",
            code="AI_STRUCTURED_OUTPUT_INVALID",
        ) from error

    async def answer_stream(self, request: dict):
        if not self.client:
            raise AiError("未配置 Anthropic API Key")
        developer = (
            "你是绑定当前小节的答疑助手。当前线程完整历史权重最高，"
            "其他线程摘要只在相关时使用。只回答锚定内容块及必要前置，"
            "不替用户答测验。输出简洁准确中文，可使用 Markdown 的短标题、"
            "列表、表格和代码块。只输出答案正文。"
        )
        body = {
            "model": self.model,
            "system": developer,
            "messages": [
                {
                    "role": "user",
                    "content": json.dumps(request, ensure_ascii=False),
                }
            ],
            "max_tokens": 2200,
            "stream": True,
        }
        if self.capabilities.reasoning_mode == "required":
            body["thinking"] = {
                "type": "enabled",
                "budget_tokens": 600,
            }
        try:
            async with self.client.stream(
                "POST",
                "v1/messages",
                json=body,
            ) as response:
                response.raise_for_status()
                event_name = ""
                async for line in response.aiter_lines():
                    if line.startswith("event:"):
                        event_name = line[6:].strip()
                        continue
                    if not line.startswith("data:"):
                        continue
                    event = json.loads(line[5:].strip())
                    if event_name == "content_block_delta":
                        delta = event.get("delta", {})
                        if delta.get("type") == "text_delta":
                            text = delta.get("text", "")
                            if text:
                                yield text
                    if event_name == "message_delta":
                        self._record_anthropic_usage(event.get("usage"))
        except httpx.HTTPStatusError as error:
            status = error.response.status_code
            raise AiError(
                "AI 服务拒绝了当前请求，请检查协议、地址、模型和密钥",
                code="AI_PROVIDER_REJECTED_REQUEST",
                retryable=status == 429 or status >= 500,
            ) from error
        except httpx.HTTPError as error:
            raise AiError(
                "无法连接 AI 服务，请稍后重试",
                code="AI_PROVIDER_UNAVAILABLE",
                retryable=True,
            ) from error

    def _record_anthropic_usage(self, usage):
        if not usage:
            return
        self.input_tokens += int(usage.get("input_tokens", 0) or 0)
        self.output_tokens += int(usage.get("output_tokens", 0) or 0)
