from pathlib import Path
from typing import Literal
from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[4]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(ROOT / "config", ROOT / ".env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )
    openai_api_key: str = Field(default="", validation_alias=AliasChoices("OPENAI_API_KEY", "apikey"))
    openai_base_url: str = Field(default="", validation_alias=AliasChoices("OPENAI_BASE_URL", "url"))
    openai_model: str = Field(default="gpt-5", validation_alias=AliasChoices("OPENAI_MODEL", "model"))
    ai_provider_protocol: Literal["openai", "anthropic"] = "openai"
    openai_api_mode: Literal["responses", "chat_completions"] = "responses"
    openai_reasoning_mode: Literal["optional", "required", "disabled"] = "optional"
    database_url: str = f"sqlite+pysqlite:///{ROOT / 'data' / 'slow-v0.db'}"
    attachment_storage_dir: Path = ROOT / "data" / "attachments"
    attachment_max_bytes: int = 10 * 1024 * 1024
    runtime_ai_config_path: Path = ROOT / "data" / "runtime-ai.json"
    web_origin: str = "http://127.0.0.1:5173"
    app_mode: Literal["development", "demo", "production", "test"] = "development"
    auth_mode: Literal["demo", "oidc"] = "demo"
    session_cookie_name: str = "slow_session"
    session_ttl_seconds: int = 60 * 60 * 24 * 14
    session_cookie_secure: bool = False
    oidc_issuer: str = ""
    oidc_client_id: str = ""
    oidc_client_secret: str = ""
    oidc_redirect_uri: str = "http://127.0.0.1:8000/api/auth/callback"
    oidc_scopes: str = "openid email profile"


settings = Settings()
