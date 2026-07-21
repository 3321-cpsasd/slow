from pathlib import Path
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
    database_url: str = f"sqlite+pysqlite:///{ROOT / 'data' / 'slow-v0.db'}"
    attachment_storage_dir: Path = ROOT / "data" / "attachments"
    attachment_max_bytes: int = 10 * 1024 * 1024
    web_origin: str = "http://127.0.0.1:5173"


settings = Settings()
