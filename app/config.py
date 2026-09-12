"""Application configuration loaded from environment variables / .env."""

from __future__ import annotations

from functools import lru_cache

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All runtime configuration for HookPing.

    Every value can be provided as an environment variable with the
    ``HOOKPING_`` prefix (for example ``HOOKPING_DATABASE_URL``). A few
    well-known variables (``TELEGRAM_BOT_TOKEN``, ``PUBLIC_BASE_URL``)
    are also accepted without the prefix for convenience.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="HOOKPING_",
        extra="ignore",
    )

    database_url: str = "sqlite:///./data/hookping.db"

    telegram_bot_token: str = Field(
        default="",
        validation_alias=AliasChoices("TELEGRAM_BOT_TOKEN", "HOOKPING_TELEGRAM_BOT_TOKEN"),
    )
    telegram_bot_username: str = "hook_ping_bot"
    telegram_api_base: str = "https://api.telegram.org"
    telegram_timeout_seconds: float = 10.0
    telegram_polling: bool = True
    telegram_poll_timeout_seconds: int = 25

    public_base_url: str = Field(
        default="",
        validation_alias=AliasChoices("PUBLIC_BASE_URL", "HOOKPING_PUBLIC_BASE_URL"),
    )

    max_body_bytes: int = 256 * 1024
    max_events_per_inbox: int = 200
    recent_events_limit: int = 50

    log_level: str = "INFO"

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.telegram_bot_token)


@lru_cache
def get_settings() -> Settings:
    return Settings()
