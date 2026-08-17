import json
import stat

import pytest
from fastapi.testclient import TestClient

from app.ai.port import ProviderCapabilities
from app.api.schemas import AiRuntimeUpdate
from app.core.config import settings
from app.main import create_app, fallback_model_profiles
from app.services.attachment_storage import LocalAttachmentStorage
from app.services.runtime_settings import RuntimeSettingsStore
from app.services.source_verifier import AcceptingSourceVerifier


def saved_demo_runtime():
    return {
        "mode": "demo",
        "api_key": "server-only-secret",
        "base_url": "https://provider.example/v1",
        "provider_model": "provider-model",
        "capabilities": ProviderCapabilities(
            protocol="openai",
            api_mode="responses",
            structured_output=True,
            streaming=True,
            reasoning_mode="optional",
        ),
        "fallbacks": [
            {
                "model": "qwen3.8-max-preview",
                "providerProtocol": "openai",
                "apiMode": "responses",
                "reasoningMode": "optional",
                "apiKey": "fallback-server-only-secret",
                "baseUrl": "https://qwen.example/v1",
            },
            {
                "model": "kimi/kimi-k3",
                "providerProtocol": "openai",
                "apiMode": "chat_completions",
                "reasoningMode": "required",
            },
        ],
        "routes": {
            "lesson_author": ["provider-model"],
            "ask_ai": ["qwen3.8-max-preview"],
        },
    }


def test_runtime_settings_round_trip_with_private_file_permissions(tmp_path):
    path = tmp_path / "runtime-ai.json"
    store = RuntimeSettingsStore(path)

    store.save(saved_demo_runtime())
    restored = store.load()

    assert restored["mode"] == "demo"
    assert restored["api_key"] == "server-only-secret"
    assert restored["provider_model"] == "provider-model"
    assert [item["model"] for item in restored["fallbacks"]] == [
        "qwen3.8-max-preview",
        "kimi/kimi-k3",
    ]
    assert restored["fallbacks"][0]["apiKey"] == "fallback-server-only-secret"
    assert restored["routes"] == {
        "lesson_author": ["provider-model"],
        "ask_ai": ["qwen3.8-max-preview"],
    }
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_runtime_settings_v4_round_trip_deployment_registry(tmp_path):
    path = tmp_path / "runtime-ai.json"
    store = RuntimeSettingsStore(path)
    runtime = saved_demo_runtime()
    runtime.update({
        "config_version_id": "config-v4",
        "route_policy_version": "policy-v2",
        "deployments": [{
            "deploymentId": "author-a",
            "providerId": "provider-a",
            "model": "qwen3.8-max",
            "modelFamilyId": "qwen",
            "providerProtocol": "openai",
            "apiMode": "chat_completions",
            "reasoningMode": "optional",
            "apiKey": "deployment-secret",
            "baseUrl": "https://provider.example/v1",
            "structuredMode": "json_object",
            "streaming": True,
            "backendAllowed": True,
            "allowedEnvironments": ["development", "test"],
            "status": "active",
        }],
        "routes": {
            "lesson_author": ["author-a"],
            "ask_ai": ["author-a"],
        },
    })

    store.save(runtime)
    restored = store.load()

    assert restored["config_version_id"] == "config-v4"
    assert restored["route_policy_version"] == "policy-v2"
    assert restored["deployments"][0]["deploymentId"] == "author-a"
    assert restored["deployments"][0]["apiKey"] == "deployment-secret"
    assert restored["routes"]["lesson_author"] == ["author-a"]


def test_runtime_settings_accepts_per_deployment_keys_without_global_key(
    tmp_path,
):
    path = tmp_path / "runtime-ai.json"
    store = RuntimeSettingsStore(path)
    runtime = saved_demo_runtime()
    runtime.update({
        "mode": "provider",
        "api_key": "",
        "deployments": [{
            "deploymentId": "author-a",
            "providerId": "provider-a",
            "model": "qwen3.8-max",
            "modelFamilyId": "qwen",
            "apiKey": "deployment-only-secret",
            "structuredMode": "json_object",
        }],
        "routes": {"lesson_author": ["author-a"]},
    })

    store.save(runtime)
    restored = store.load()

    assert restored["api_key"] == ""
    assert restored["deployments"][0]["apiKey"] == (
        "deployment-only-secret"
    )


def test_runtime_update_rejects_unknown_route_purpose():
    with pytest.raises(ValueError, match="不支持的用途路由"):
        AiRuntimeUpdate.model_validate({
            "mode": "provider",
            "model": "qwen3.8-max",
            "deployments": [{
                "deploymentId": "author-a",
                "providerId": "provider-a",
                "model": "qwen3.8-max",
                "modelFamilyId": "qwen",
            }],
            "routes": {"invented_purpose": ["author-a"]},
        })


def test_runtime_settings_reject_route_to_unknown_deployment(tmp_path):
    path = tmp_path / "runtime-ai.json"
    runtime = saved_demo_runtime()
    RuntimeSettingsStore(path).save(runtime)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["deployments"] = [{
        "deploymentId": "author-a",
        "providerId": "provider-a",
        "model": "qwen3.8-max",
        "modelFamilyId": "qwen",
        "structuredMode": "json_object",
    }]
    payload["routes"] = {"ask_ai": ["missing"]}
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(RuntimeError, match="引用了未知部署"):
        RuntimeSettingsStore(path).load()


def test_fallback_profiles_exclude_disabled_bundled_models_and_normalize_qwen():
    profiles = fallback_model_profiles({
        "provider_model": "provider-model",
        "fallbacks": [
            {
                "model": "qwen3.8-max-preview",
                "providerProtocol": "openai",
                "apiMode": "responses",
                "reasoningMode": "optional",
            },
            {
                "model": "kimi/kimi-k3",
                "providerProtocol": "openai",
                "apiMode": "chat_completions",
                "reasoningMode": "required",
            },
            {
                "model": "kimi/kimi-k3",
                "providerProtocol": "openai",
                "apiMode": "chat_completions",
                "reasoningMode": "required",
            },
        ],
    })

    assert [item["model"] for item in profiles] == ["qwen3.8-max-preview"]
    assert profiles[0]["apiMode"] == "chat_completions"
    assert profiles[0]["reasoningMode"] == "required"


def test_glm_fallback_reuses_model_studio_credentials_with_thinking_disabled(
    monkeypatch,
):
    monkeypatch.setattr(settings, "ai_fallback_models", "glm-5.2")
    monkeypatch.setattr(settings, "qwen38_api_key", "model-studio-secret")
    monkeypatch.setattr(
        settings,
        "qwen38_base_url",
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
    )

    profiles = fallback_model_profiles({
        "provider_model": "deepseek-v4-flash-0731",
        "provider_protocol": "openai",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    })

    assert profiles == [{
        "model": "glm-5.2",
        "providerProtocol": "openai",
        "apiMode": "chat_completions",
        "reasoningMode": "disabled",
        "apiKey": "model-studio-secret",
        "baseUrl": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    }]


def test_app_restores_saved_runtime_without_returning_the_key(tmp_path):
    path = tmp_path / "runtime-ai.json"
    RuntimeSettingsStore(path).save(saved_demo_runtime())
    storage = LocalAttachmentStorage(tmp_path / "attachments")

    with TestClient(
        create_app(
            "sqlite+pysqlite:///:memory:",
            source_verifier=AcceptingSourceVerifier(),
            attachment_storage=storage,
            runtime_settings_path=path,
        )
    ) as client:
        response = client.get("/api/runtime/ai")

    assert response.status_code == 200
    assert response.json()["mode"] == "demo"
    assert response.json()["providerModel"] == "provider-model"
    assert response.json()["fallbackModels"] == ["qwen3.8-max-preview"]
    assert response.json()["apiKeyStored"] is True
    assert response.json()["ephemeral"] is False
    assert "server-only-secret" not in response.text
    assert "fallback-server-only-secret" not in response.text


def test_corrupt_runtime_settings_fail_closed(tmp_path):
    path = tmp_path / "runtime-ai.json"
    path.write_text('{"schemaVersion":1,"mode":"provider"}', encoding="utf-8")

    with pytest.raises(RuntimeError, match="供应商协议无效"):
        create_app(
            "sqlite+pysqlite:///:memory:",
            runtime_settings_path=path,
        )
