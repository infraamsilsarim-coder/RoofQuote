from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "sqlite:///./roofquote.db"
    secret_key: str = "change-me-in-production-use-long-random-string"
    openrouter_api_key: str = ""
    openrouter_model: str = "anthropic/claude-opus-4.6"

    @property
    def base_dir(self) -> Path:
        return Path(__file__).resolve().parent.parent

    @property
    def prompt_path(self) -> Path:
        return self.base_dir / "prompt.txt"


@lru_cache
def get_settings() -> Settings:
    return Settings()
