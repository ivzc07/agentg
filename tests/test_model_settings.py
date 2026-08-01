"""Issue #164 — No model call can hang the bot.

Every model call must carry an explicit timeout, at least one retry for
transient failures, max_tokens to cap output length, and a deliberate
temperature. This test asserts those settings actually reach the model
client at all three call sites: Agent turn, compaction summarizer, and
linking phraser.
"""

from __future__ import annotations

import litellm

from agentg.agent import build_agent
from agentg.compaction import build_summarizer
from agentg.linking import build_phraser
from agentg.config import Settings


def _settings() -> Settings:
    return Settings(
        telegram_bot_token="test-token",
        model="openai/gpt-4o-mini",
        model_api_key="not-a-real-key",
        database_url="sqlite+aiosqlite://",
    )


def _capture(content: str = "ok"):
    """Return (capture_fn, last_kwargs) for intercepting litellm.acompletion.

    The returned async function replaces ``litellm.acompletion``, stashes
    every kwarg it was called with into *last_kwargs*, and returns a
    minimal valid ``ModelResponse`` whose message content is *content*.
    """
    last_kwargs: dict[str, object] = {}

    async def capture_acompletion(**kwargs: object) -> litellm.types.utils.ModelResponse:
        last_kwargs.update(kwargs)
        from litellm.types.utils import Choices, Message, ModelResponse

        return ModelResponse(choices=[Choices(message=Message(content=content))])

    return capture_acompletion, last_kwargs


# --- Agent turn (LitellmModel via ModelSettings) ---


async def test_agent_model_settings_reach_litellm_client():
    """The Agent's timeout and num_retries must actually reach
    litellm.acompletion through the SDK pipeline, not just sit on the
    ModelSettings object.  Drives ``agent.model.get_response`` with
    ``litellm.acompletion`` patched — same pattern as the compaction
    and linking tests — so a future SDK change to extra_args handling
    turns this red instead of staying silently green."""
    from agents.models.interface import ModelTracing

    agent = build_agent(_settings())
    capture, last_kwargs = _capture()

    original = litellm.acompletion
    litellm.acompletion = capture
    try:
        await agent.model.get_response(
            system_instructions="You are a coach.",
            input="Hello",
            model_settings=agent.model_settings,
            tools=[],
            output_schema=None,
            handoffs=[],
            tracing=ModelTracing.DISABLED,
        )
    finally:
        litellm.acompletion = original

    assert "timeout" in last_kwargs, "Agent model call has no timeout set"
    assert last_kwargs["timeout"] > 0, (
        f"Agent timeout must be positive, got {last_kwargs['timeout']}"
    )
    assert "num_retries" in last_kwargs, "Agent model call has no num_retries set"
    assert last_kwargs["num_retries"] >= 1, (
        f"Agent must retry at least once, got {last_kwargs['num_retries']}"
    )
    # _skip_mcp_handler must still be present (regression from the proxy fix)
    assert last_kwargs.get("_skip_mcp_handler") is True, (
        "Agent extra_args must still skip the proxy/MCP handler"
    )


def test_agent_model_settings_have_max_tokens_and_temperature():
    """Agent replies must be length-capped and temperature set deliberately."""
    agent = build_agent(_settings())
    ms = agent.model_settings

    assert ms.max_tokens is not None, "Agent has no max_tokens cap on replies"
    assert ms.max_tokens > 0, f"Agent max_tokens must be positive, got {ms.max_tokens}"
    assert ms.temperature is not None, "Agent has no temperature set"
    assert 0 <= ms.temperature <= 2, (
        f"Agent temperature must be in [0, 2], got {ms.temperature}"
    )


# --- Compaction summarizer (direct litellm.acompletion) ---


async def test_compaction_summarizer_passes_timeout_and_retry():
    """The compaction summarizer must pass timeout and num_retries to litellm."""
    summarizer = build_summarizer(_settings())
    capture, last_kwargs = _capture(content='{"summary": "ok", "notes": []}')

    original = litellm.acompletion
    litellm.acompletion = capture
    try:
        await summarizer([{"role": "user", "content": "bench 60"}], [])
    finally:
        litellm.acompletion = original

    assert "timeout" in last_kwargs, "Compaction call has no timeout set"
    assert last_kwargs["timeout"] > 0, (
        f"Compaction timeout must be positive, got {last_kwargs['timeout']}"
    )
    assert "num_retries" in last_kwargs, "Compaction call has no num_retries set"
    assert last_kwargs["num_retries"] >= 1, (
        f"Compaction must retry at least once, got {last_kwargs['num_retries']}"
    )


async def test_compaction_summarizer_passes_max_tokens_and_temperature():
    """The compaction summarizer must cap output length and set temperature."""
    summarizer = build_summarizer(_settings())
    capture, last_kwargs = _capture(content='{"summary": "ok", "notes": []}')

    original = litellm.acompletion
    litellm.acompletion = capture
    try:
        await summarizer([{"role": "user", "content": "bench 60"}], [])
    finally:
        litellm.acompletion = original

    assert "max_tokens" in last_kwargs, "Compaction call has no max_tokens set"
    assert last_kwargs["max_tokens"] > 0, (
        f"Compaction max_tokens must be positive, got {last_kwargs['max_tokens']}"
    )
    assert "temperature" in last_kwargs, "Compaction call has no temperature set"
    assert 0 <= last_kwargs["temperature"] <= 2, (
        f"Compaction temperature must be in [0, 2], got {last_kwargs['temperature']}"
    )


# --- Linking phraser (direct litellm.acompletion) ---


async def test_linking_phraser_passes_timeout_and_retry():
    """The linking phraser must pass timeout and num_retries to litellm."""
    phraser = build_phraser(_settings())
    capture, last_kwargs = _capture(content="Hola, bienvenido!")

    original = litellm.acompletion
    litellm.acompletion = capture
    try:
        await phraser("Welcome them", "hola")
    finally:
        litellm.acompletion = original

    assert "timeout" in last_kwargs, "Linking phraser call has no timeout set"
    assert last_kwargs["timeout"] > 0, (
        f"Linking timeout must be positive, got {last_kwargs['timeout']}"
    )
    assert "num_retries" in last_kwargs, "Linking phraser call has no num_retries set"
    assert last_kwargs["num_retries"] >= 1, (
        f"Linking must retry at least once, got {last_kwargs['num_retries']}"
    )


async def test_linking_phraser_passes_max_tokens_and_temperature():
    """The linking phraser must cap output length and set temperature."""
    phraser = build_phraser(_settings())
    capture, last_kwargs = _capture(content="Hola, bienvenido!")

    original = litellm.acompletion
    litellm.acompletion = capture
    try:
        await phraser("Welcome them", "hola")
    finally:
        litellm.acompletion = original

    assert "max_tokens" in last_kwargs, "Linking phraser call has no max_tokens set"
    assert last_kwargs["max_tokens"] > 0, (
        f"Linking max_tokens must be positive, got {last_kwargs['max_tokens']}"
    )
    assert "temperature" in last_kwargs, "Linking phraser call has no temperature set"
    assert 0 <= last_kwargs["temperature"] <= 2, (
        f"Linking temperature must be in [0, 2], got {last_kwargs['temperature']}"
    )
