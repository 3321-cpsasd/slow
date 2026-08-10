import json
import os
import tempfile
from pathlib import Path


class RuntimeSettingsStore:
    """Server-only persistence for the locally selected AI runtime."""

    SCHEMA_VERSION = 2

    def __init__(self, path: Path):
        self.path = path

    def load(self) -> dict | None:
        if not self.path.exists():
            return None
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise RuntimeError("本机 AI 配置损坏，无法安全启动") from error
        schema_version = payload.get("schemaVersion")
        if schema_version not in {1, self.SCHEMA_VERSION}:
            raise RuntimeError("本机 AI 配置版本不受支持")
        mode = payload.get("mode")
        protocol = payload.get("providerProtocol")
        api_mode = payload.get("apiMode")
        reasoning_mode = payload.get("reasoningMode")
        if mode not in {"provider", "demo"}:
            raise RuntimeError("本机 AI 配置运行模式无效")
        if protocol not in {"openai", "anthropic"}:
            raise RuntimeError("本机 AI 配置供应商协议无效")
        if api_mode not in {"responses", "chat_completions", "messages"}:
            raise RuntimeError("本机 AI 配置接口形态无效")
        if reasoning_mode not in {"optional", "required", "disabled"}:
            raise RuntimeError("本机 AI 配置推理模式无效")
        values = {
            "mode": mode,
            "api_key": str(payload.get("apiKey") or ""),
            "base_url": str(payload.get("baseUrl") or ""),
            "provider_model": str(payload.get("model") or ""),
            "provider_protocol": protocol,
            "api_mode": api_mode,
            "reasoning_mode": reasoning_mode,
            "fallbacks": list(payload.get("fallbackModels") or []),
        }
        for fallback in values["fallbacks"]:
            if not isinstance(fallback, dict) or not str(fallback.get("model") or ""):
                raise RuntimeError("本机 AI 备用模型配置无效")
            if fallback.get("providerProtocol", "openai") not in {"openai", "anthropic"}:
                raise RuntimeError("本机 AI 备用模型协议无效")
            if fallback.get("apiMode") not in {
                "responses", "chat_completions", "messages"
            }:
                raise RuntimeError("本机 AI 备用模型接口形态无效")
            if fallback.get("reasoningMode") not in {
                "optional", "required", "disabled"
            }:
                raise RuntimeError("本机 AI 备用模型推理模式无效")
            if not isinstance(fallback.get("apiKey", ""), str):
                raise RuntimeError("本机 AI 备用模型密钥配置无效")
            if not isinstance(fallback.get("baseUrl", ""), str):
                raise RuntimeError("本机 AI 备用模型地址配置无效")
        if not values["provider_model"]:
            raise RuntimeError("本机 AI 配置模型名称为空")
        if mode == "provider" and not values["api_key"]:
            raise RuntimeError("本机 AI 配置缺少 API Key")
        os.chmod(self.path, 0o600)
        return values

    def save(self, runtime: dict) -> None:
        capabilities = runtime["capabilities"]
        payload = {
            "schemaVersion": self.SCHEMA_VERSION,
            "mode": runtime["mode"],
            "apiKey": runtime["api_key"],
            "baseUrl": runtime["base_url"],
            "model": runtime["provider_model"],
            "providerProtocol": capabilities.protocol,
            "apiMode": capabilities.api_mode,
            "reasoningMode": capabilities.reasoning_mode,
            "fallbackModels": runtime.get("fallbacks", []),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".runtime-ai-",
            dir=self.path.parent,
            text=True,
        )
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, self.path)
            os.chmod(self.path, 0o600)
        except Exception:
            try:
                os.close(descriptor)
            except OSError:
                pass
            try:
                os.unlink(temporary_name)
            except OSError:
                pass
            raise
