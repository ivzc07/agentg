"""Environment-driven configuration: dev bot token, model key, database URL."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

DEFAULT_MODEL = "openai/gpt-4o-mini"
DEFAULT_DATABASE_URL = "postgresql+asyncpg://agentg:agentg@localhost:5432/agentg"
DEFAULT_DEMO_MEDIA_ROOT = "/data/demos"  # where the canonical demo MP4s live
DEFAULT_DASHBOARD_PORT = 8080
DEFAULT_FORGET_ME_CONFIRMATION_SECONDS = 300  # 5 minutes
DEFAULT_STALE_LEASE_SECONDS = 30  # 30 seconds — with heartbeat renewal, long enough

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
    # Optional override for the Vite-built React bundle directory; defaults
    # to the repo-relative path ``frontend/dist/``. Set this in container
    # deploys where the package is installed into site-packages.
    dashboard_spa_dist: str = ""
    # How long a forget-me confirmation phrase stays valid (issue #212).
    forget_me_confirmation_seconds: int = DEFAULT_FORGET_ME_CONFIRMATION_SECONDS
    # How long a model-turn lease lives before it is considered stale and
    # reclaimable by another runtime (issue #212, fix-r20).  Must be shorter
    # than forget_me_confirmation_seconds so a crashed lease can be reclaimed
    # before the confirmation phrase expires.
    stale_lease_seconds: int = DEFAULT_STALE_LEASE_SECONDS

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        if env is None:
            env = os.environ
        missing = [name for name in REQUIRED_VARS if not env.get(name)]
        if missing:
            raise ConfigError(f"missing required environment variables: {', '.join(missing)}")
        try:
            port = int(env.get("DASHBOARD_PORT") or DEFAULT_DASHBOARD_PORT)
        except ValueError:
            raise ConfigError(
                f"DASHBOARD_PORT must be a number, got {env.get('DASHBOARD_PORT')!r}"
            )
        forget_me = _int_env(
            env, "FORGET_ME_CONFIRMATION_SECONDS", DEFAULT_FORGET_ME_CONFIRMATION_SECONDS
        )
        if forget_me < 60:
            raise ConfigError(
                f"FORGET_ME_CONFIRMATION_SECONDS must be at least 60, got {forget_me!r}"
            )
        stale_lease = _int_env(
            env, "STALE_LEASE_SECONDS", DEFAULT_STALE_LEASE_SECONDS
        )
        if stale_lease < 30:
            raise ConfigError(
                f"STALE_LEASE_SECONDS must be at least 30, got {stale_lease!r}"
            )
        if stale_lease >= forget_me:
            raise ConfigError(
                f"STALE_LEASE_SECONDS ({stale_lease}) must be shorter than "
                f"FORGET_ME_CONFIRMATION_SECONDS ({forget_me}) so a crashed "
                f"lease can be reclaimed before the confirmation phrase expires"
            )
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
            dashboard_spa_dist=env.get("DASHBOARD_SPA_DIST") or "",
            forget_me_confirmation_seconds=forget_me,
            stale_lease_seconds=stale_lease,
        )


def _int_env(env: Mapping[str, str], key: str, default: int) -> int:
    """Parse an optional integer env var, falling back to *default*."""
    raw = env.get(key)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        raise ConfigError(f"{key} must be a number, got {raw!r}")


def _as_asyncpg_url(url: str) -> str:
    """SQLAlchemy needs the async driver spelled out; accept plain postgres URLs too."""
    for prefix in ("postgresql://", "postgres://"):
        if url.startswith(prefix):
            return "postgresql+asyncpg://" + url.removeprefix(prefix)
    return url
