"""Environment-driven settings for the agent service.

All configuration comes from the environment / a local ``.env`` file (see
``.env.example``). Secrets are never hardcoded here and never logged.
"""

from functools import lru_cache

from pydantic import Field, SecretStr
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

    # Internal app-shell <-> agent API. The app shell is the single refresh authority
    # for Shopify offline tokens; the agent calls back to it for short-lived tokens.
    # SecretStr so the key can never surface in a repr, log line, or traceback.
    internal_api_key: SecretStr = Field(default=SecretStr(""))
    app_shell_url: str = "http://localhost:3000"

    # Shopify Admin API version — must match `api_version` in app/shopify.app.toml.
    shopify_api_version: str = "2026-07"

    # AI shopping-engine API keys (optional until Phase 2 wiring; no secret defaults).
    # SecretStr so the key can never surface in a repr, log line, or traceback.
    perplexity_api_key: SecretStr = Field(default=SecretStr(""))
    openai_api_key: str | None = Field(default=None)
    gemini_api_key: str | None = Field(default=None)

    # EngineRunner / Perplexity Sonar knobs.
    perplexity_model: str = "sonar"
    # Pinned, not left to the engine default: share-of-model is a cross-period metric, so an
    # unpinned temperature would make historical engine_runs non-comparable.
    perplexity_temperature: float = 0.2
    perplexity_timeout_seconds: float = 30.0
    engine_max_concurrency: int = 5
    engine_max_retries: int = 3


@lru_cache
def get_settings() -> Settings:
    """Return a cached Settings instance."""
    return Settings()
