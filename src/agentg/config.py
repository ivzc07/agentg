"""Environment-driven configuration: dev bot token, model key, database URL."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

DEFAULT_MODEL = "openai/gpt-4o-mini"
DEFAULT_DATABASE_URL = "postgresql+asyncpg://agentg:agentg@localhost:5432/agentg"

REQUIRED_VARS = ("TELEGRAM_BOT_TOKEN", "MODEL_API_KEY")


class ConfigError(Exception):
    """A required environment variable is missing."""


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str
    model: str
    model_api_key: str
    database_url: str

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        if env is None:
            env = os.environ
        missing = [name for name in REQUIRED_VARS if not env.get(name)]
        if missing:
            raise ConfigError(f"missing required environment variables: {', '.join(missing)}")
        return cls(
            telegram_bot_token=env["TELEGRAM_BOT_TOKEN"],
            model=env.get("MODEL", DEFAULT_MODEL),
            model_api_key=env["MODEL_API_KEY"],
            database_url=_as_asyncpg_url(env.get("DATABASE_URL", DEFAULT_DATABASE_URL)),
        )


def _as_asyncpg_url(url: str) -> str:
    """SQLAlchemy needs the async driver spelled out; accept plain postgres URLs too."""
    for prefix in ("postgresql://", "postgres://"):
        if url.startswith(prefix):
            return "postgresql+asyncpg://" + url.removeprefix(prefix)
    return url
