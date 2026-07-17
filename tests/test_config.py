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
