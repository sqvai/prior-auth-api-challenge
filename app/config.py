# Copyright (c) 2026 John Harris. All rights reserved. Proprietary.

from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings. Environment wins over .env. See .env.example."""

    database_url: str = "postgresql+psycopg://priorauth:priorauth@127.0.0.1:54329/priorauth"
    token_secret: str = "dev-only-change-me"
    ai_base_url: str | None = None
    ai_api_key: str | None = None
    ai_model: str = "documentation-checker"

    model_config = SettingsConfigDict(env_prefix="PRIOR_AUTH_", env_file=".env", extra="ignore")

    @field_validator("ai_base_url", "ai_api_key", mode="before")
    @classmethod
    def blank_is_unset(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
