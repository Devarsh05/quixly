"""Environment-driven settings for the agent service.

All configuration comes from the environment / a local ``.env`` file (see
``.env.example``). Secrets are never hardcoded here and never logged.
"""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Runtime
    environment: str = "development"
    log_level: str = "info"

    # Infrastructure
    database_url: str = "postgresql+asyncpg://quixly:quixly@localhost:5432/quixly"
    redis_url: str = "redis://localhost:6379/0"

    # AI shopping-engine API keys (optional until Phase 2 wiring; no secret defaults)
    perplexity_api_key: str | None = Field(default=None)
    openai_api_key: str | None = Field(default=None)
    gemini_api_key: str | None = Field(default=None)


@lru_cache
def get_settings() -> Settings:
    """Return a cached Settings instance."""
    return Settings()
