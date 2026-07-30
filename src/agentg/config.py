"""Environment-driven configuration: dev bot token, model key, database URL."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

DEFAULT_MODEL = "openai/gpt-4o-mini"
DEFAULT_DATABASE_URL = "postgresql+asyncpg://agentg:agentg@localhost:5432/agentg"
DEFAULT_DEMO_MEDIA_ROOT = "/data/demos"  # where the canonical demo MP4s live
DEFAULT_DASHBOARD_PORT = 8080

REQUIRED_VARS = ("TELEGRAM_BOT_TOKEN", "MODEL_API_KEY")


class ConfigError(Exception):
    """A required environment variable is missing."""


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str
    model: str
    model_api_key: str
    database_url: str
    demo_media_root: str = DEFAULT_DEMO_MEDIA_ROOT
    # Public origin the /dashboard magic links point at (spec-dashboard
    # §Stack); defaults to the local server for dev.
    dashboard_base_url: str = f"http://localhost:{DEFAULT_DASHBOARD_PORT}"
    dashboard_port: int = DEFAULT_DASHBOARD_PORT
    # HMAC key for the session cookie; falls back to the bot token (already
    # a stable per-deploy secret) when unset.
    dashboard_session_secret: str | None = None

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        if env is None:
            env = os.environ
        missing = [name for name in REQUIRED_VARS if not env.get(name)]
        if missing:
            raise ConfigError(f"missing required environment variables: {', '.join(missing)}")
        port = int(env.get("DASHBOARD_PORT") or DEFAULT_DASHBOARD_PORT)
        return cls(
            telegram_bot_token=env["TELEGRAM_BOT_TOKEN"],
            model=env.get("MODEL") or DEFAULT_MODEL,
            model_api_key=env["MODEL_API_KEY"],
            database_url=_as_asyncpg_url(env.get("DATABASE_URL") or DEFAULT_DATABASE_URL),
            demo_media_root=env.get("DEMO_MEDIA_ROOT") or DEFAULT_DEMO_MEDIA_ROOT,
            dashboard_base_url=(
                env.get("DASHBOARD_BASE_URL") or f"http://localhost:{port}"
            ).rstrip("/"),
            dashboard_port=port,
            dashboard_session_secret=env.get("DASHBOARD_SESSION_SECRET") or None,
        )


def _as_asyncpg_url(url: str) -> str:
    """SQLAlchemy needs the async driver spelled out; accept plain postgres URLs too."""
    for prefix in ("postgresql://", "postgres://"):
        if url.startswith(prefix):
            return "postgresql+asyncpg://" + url.removeprefix(prefix)
    return url
