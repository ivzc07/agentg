"""Tests for agentg.main — process entrypoint and shutdown logic."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from agentg.main import _shutdown


@pytest.fixture
def mock_scheduler():
    sched = MagicMock()
    sched.shutdown = MagicMock()
    return sched


@pytest.fixture
def mock_bot_session():
    session = MagicMock()
    session.close = AsyncMock()
    return session


@pytest.fixture
def mock_bot(mock_bot_session):
    bot = MagicMock()
    bot.session = mock_bot_session
    return bot


@pytest.fixture
def mock_web_runner():
    runner = MagicMock()
    runner.cleanup = AsyncMock()
    return runner


async def test_shutdown_stops_scheduler_closes_bot_session_and_cleans_up_web_runner(
    mock_scheduler, mock_bot, mock_web_runner
):
    """_shutdown must call shutdown on the scheduler, close the bot's HTTP
    session, and clean up the web runner."""
    await _shutdown(mock_scheduler, mock_bot, mock_web_runner)

    # Scheduler: shutdown() called with wait=False
    mock_scheduler.shutdown.assert_called_once_with(wait=False)

    # Bot: session.close() awaited
    mock_bot.session.close.assert_awaited_once()

    # Web runner: cleanup() awaited
    mock_web_runner.cleanup.assert_awaited_once()


async def test_shutdown_cleans_up_web_runner_even_when_bot_session_close_raises(
    mock_scheduler, mock_bot, mock_web_runner
):
    """_shutdown must still call web_runner.cleanup() when bot.session.close()
    raises — the finally guard prevents a failing close from skipping cleanup."""
    mock_bot.session.close.side_effect = RuntimeError("connection lost")

    with pytest.raises(RuntimeError, match="connection lost"):
        await _shutdown(mock_scheduler, mock_bot, mock_web_runner)

    # Scheduler shutdown still happens (it runs first, before the raise)
    mock_scheduler.shutdown.assert_called_once_with(wait=False)
    # Despite the failing close, cleanup must still be called
    mock_web_runner.cleanup.assert_awaited_once()
