import json
import os
import tempfile
from pathlib import Path


class RuntimeSettingsStore:
    """Server-only persistence for the locally selected AI runtime."""

    SCHEMA_VERSION = 4

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
        if schema_version not in {1, 2, 3, self.SCHEMA_VERSION}:
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
            "routes": dict(payload.get("routes") or {}),
            "deployments": list(payload.get("deployments") or []),
            "config_version_id": str(
                payload.get("configVersionId") or f"runtime-v{schema_version}"
            ),
            "route_policy_version": str(
                payload.get("routePolicyVersion") or "ai_route_v1"
            ),
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
        allowed_routes = {
            "default", "curriculum", "lesson_author", "ask_ai",
            "feedback_style", "feedback_accuracy", "assessment_probe",
            "assessment_evaluation", "note", "source_repair",
            "source_review", "quality_review", "assessment",
            "feedback_resolution",
        }
        for purpose, models in values["routes"].items():
            if purpose not in allowed_routes:
                raise RuntimeError("本机 AI 路由用途无效")
            if (
                not isinstance(models, list)
                or not models
                or any(not isinstance(model, str) or not model for model in models)
            ):
                raise RuntimeError("本机 AI 路由模型列表无效")
        deployment_ids = set()
        for deployment in values["deployments"]:
            if not isinstance(deployment, dict):
                raise RuntimeError("本机 AI 模型部署配置无效")
            deployment_id = str(
                deployment.get("deploymentId") or ""
            ).strip()
            provider_id = str(
                deployment.get("providerId") or ""
            ).strip()
            family_id = str(
                deployment.get("modelFamilyId") or ""
            ).strip()
            model = str(deployment.get("model") or "").strip()
            if (
                not deployment_id
                or not provider_id
                or not family_id
                or not model
            ):
                raise RuntimeError("本机 AI 模型部署缺少稳定身份")
            deployment["deploymentId"] = deployment_id
            deployment["providerId"] = provider_id
            deployment["modelFamilyId"] = family_id
            deployment["model"] = model
            if deployment_id in deployment_ids:
                raise RuntimeError("本机 AI 模型部署 ID 重复")
            deployment_ids.add(deployment_id)
            if deployment.get("structuredMode") not in {
                "native_schema", "json_object", "prompt_json", "unsupported"
            }:
                raise RuntimeError("本机 AI 模型部署结构化能力无效")
            if deployment.get("providerProtocol", "openai") not in {
                "openai", "anthropic"
            }:
                raise RuntimeError("本机 AI 模型部署协议无效")
            if deployment.get("apiMode", "chat_completions") not in {
                "responses", "chat_completions", "messages"
            }:
                raise RuntimeError("本机 AI 模型部署接口形态无效")
            if deployment.get("reasoningMode", "optional") not in {
                "optional", "required", "disabled"
            }:
                raise RuntimeError("本机 AI 模型部署推理模式无效")
            if deployment.get("status", "active") not in {
                "active", "quarantined", "disabled"
            }:
                raise RuntimeError("本机 AI 模型部署状态无效")
            if not isinstance(deployment.get("streaming", True), bool):
                raise RuntimeError("本机 AI 模型部署流式能力无效")
            if not isinstance(deployment.get("backendAllowed", False), bool):
                raise RuntimeError("本机 AI 模型部署授权标记无效")
            environments = deployment.get(
                "allowedEnvironments", ["development", "test"]
            )
            if (
                not isinstance(environments, list)
                or not environments
                or any(
                    item not in {
                        "development", "demo", "test", "production"
                    }
                    for item in environments
                )
            ):
                raise RuntimeError("本机 AI 模型部署环境范围无效")
            if not isinstance(deployment.get("apiKey", ""), str):
                raise RuntimeError("本机 AI 模型部署密钥配置无效")
            if not isinstance(deployment.get("baseUrl", ""), str):
                raise RuntimeError("本机 AI 模型部署地址配置无效")
        if deployment_ids:
            for purpose, route in values["routes"].items():
                if any(item not in deployment_ids for item in route):
                    raise RuntimeError(
                        f"本机 AI 路由 {purpose} 引用了未知部署"
                    )
        if not values["provider_model"]:
            raise RuntimeError("本机 AI 配置模型名称为空")
        deployment_keys_complete = bool(values["deployments"]) and all(
            str(item.get("apiKey") or "").strip()
            for item in values["deployments"]
        )
        if (
            mode == "provider"
            and not values["api_key"]
            and not deployment_keys_complete
        ):
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
            "routes": runtime.get("routes", {}),
            "deployments": runtime.get("deployments", []),
            "configVersionId": runtime.get("config_version_id", "runtime-legacy"),
            "routePolicyVersion": runtime.get(
                "route_policy_version", "ai_route_v1"
            ),
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
