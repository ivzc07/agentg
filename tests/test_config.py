"""Settings.from_env: required vars, defaults, database-URL normalization."""

import pytest

from agentg.config import ConfigError, Settings

FULL_ENV = {
    "TELEGRAM_BOT_TOKEN": "123:abc",
    "MODEL_API_KEY": "sk-test",
    "MODEL": "anthropic/claude-sonnet-5",
    "DATABASE_URL": "postgresql+asyncpg://u:p@host:5432/db",
}


def test_reads_all_values():
    settings = Settings.from_env(FULL_ENV)
    assert settings.telegram_bot_token == "123:abc"
    assert settings.model_api_key == "sk-test"
    assert settings.model == "anthropic/claude-sonnet-5"
    assert settings.database_url == "postgresql+asyncpg://u:p@host:5432/db"


@pytest.mark.parametrize("missing", ["TELEGRAM_BOT_TOKEN", "MODEL_API_KEY"])
def test_missing_required_var_is_named_in_the_error(missing):
    env = {name: value for name, value in FULL_ENV.items() if name != missing}
    with pytest.raises(ConfigError, match=missing):
        Settings.from_env(env)


def test_model_and_database_url_have_defaults():
    settings = Settings.from_env({"TELEGRAM_BOT_TOKEN": "123:abc", "MODEL_API_KEY": "sk-test"})
    assert settings.model
    assert settings.database_url.startswith("postgresql+asyncpg://")


@pytest.mark.parametrize("url", ["postgres://u:p@h/db", "postgresql://u:p@h/db"])
def test_sync_postgres_urls_are_normalized_to_the_async_driver(url):
    settings = Settings.from_env({**FULL_ENV, "DATABASE_URL": url})
    assert settings.database_url == "postgresql+asyncpg://u:p@h/db"


def test_empty_optional_vars_fall_back_to_defaults():
    settings = Settings.from_env({**FULL_ENV, "MODEL": "", "DATABASE_URL": ""})
    assert settings.model
    assert settings.database_url.startswith("postgresql+asyncpg://")


def test_non_postgres_urls_pass_through_unchanged():
    url = "sqlite+aiosqlite:///tmp/x.db"
    settings = Settings.from_env({**FULL_ENV, "DATABASE_URL": url})
    assert settings.database_url == url


def test_dashboard_settings_default_to_a_local_server():
    settings = Settings.from_env({"TELEGRAM_BOT_TOKEN": "123:abc", "MODEL_API_KEY": "sk-test"})
    assert settings.dashboard_base_url == "http://localhost:8080"
    assert settings.dashboard_port == 8080
    assert settings.dashboard_session_secret is None  # falls back to the bot token


def test_dashboard_settings_read_their_env_vars():
    settings = Settings.from_env(
        {
            **FULL_ENV,
            "DASHBOARD_BASE_URL": "https://dash.example.com/",
            "DASHBOARD_PORT": "9090",
            "DASHBOARD_SESSION_SECRET": "s3cret",
        }
    )
    assert settings.dashboard_base_url == "https://dash.example.com"  # no trailing slash
    assert settings.dashboard_port == 9090
    assert settings.dashboard_session_secret == "s3cret"


def test_spa_enabled_default_is_false():
    """The SPA flag must default to False so production is unaffected (ADR 0004)."""
    settings = Settings.from_env(FULL_ENV)
    assert settings.dashboard_spa_enabled is False


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("1", True),
        ("true", True),
        ("TRUE", True),
        (" yes ", True),
        ("0", False),
        ("false", False),
        ("", False),
        ("no", False),
    ],
)
def test_spa_enabled_bool_env_table(raw, expected):
    """_bool_env parses a boolean environment variable with the expected
    truthy/falsy table."""
    settings = Settings.from_env({**FULL_ENV, "DASHBOARD_SPA_ENABLED": raw})
    assert settings.dashboard_spa_enabled is expected


def test_spa_dist_default_is_empty():
    """dashboard_spa_dist defaults to empty so the repo-relative path is used."""
    settings = Settings.from_env(FULL_ENV)
    assert settings.dashboard_spa_dist == ""


def test_spa_dist_reads_from_env():
    """DASHBOARD_SPA_DIST overrides the SPA bundle directory for container deploys."""
    settings = Settings.from_env(
        {**FULL_ENV, "DASHBOARD_SPA_DIST": "/app/frontend/dist"}
    )
    assert settings.dashboard_spa_dist == "/app/frontend/dist"
