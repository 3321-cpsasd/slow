import stat

import pytest
from fastapi.testclient import TestClient

from app.ai.port import ProviderCapabilities
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
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_fallback_profiles_exclude_primary_model_and_duplicates():
    profiles = fallback_model_profiles({
        "provider_model": "qwen3.8-max-preview",
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

    assert [item["model"] for item in profiles] == ["kimi/kimi-k3"]


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
    assert response.json()["fallbackModels"] == [
        "qwen3.8-max-preview",
        "kimi/kimi-k3",
    ]
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
