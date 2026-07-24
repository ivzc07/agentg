"""The Agent must invoke litellm in a way that runs without the proxy extras.

Regression for the deploy where every turn crashed with
``APIConnectionError: ... No module named 'fastapi'`` and linked Members only
ever got "something went wrong". litellm 1.92 unconditionally imports its
proxy/MCP handler chain whenever ``tools`` are present — and that chain needs
``fastapi``/``orjson``/… that a normal ``openai-agents[litellm]`` install does
not ship. The Agent always sends tools, so it hit that import on every turn.

Linking never calls the model, so linking still worked — which is exactly
why the gap slipped past the existing tests (they all monkeypatch
``Runner.run``, never touching the real litellm path).

This drives litellm the way the runner does — async, with tools, on the
configured model — but crucially it applies the *same* ``extra_args`` that
``build_agent`` puts on the Agent's model settings (the SDK forwards those
straight to ``litellm.acompletion``). So the test goes red unless the Agent is
configured to skip the proxy import. An unroutable ``api_base`` keeps it offline
and fast; the import that broke fires during request setup, before any network
call.
"""

from __future__ import annotations

import litellm

from agentg.agent import build_agent
from agentg.config import DEFAULT_MODEL, Settings

# The Agent always runs with tools (build_tools); that is the exact litellm code
# path that pulled in the missing dependency.
_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "noop",
            "description": "placeholder",
            "parameters": {"type": "object", "properties": {}},
        },
    }
]


def _settings() -> Settings:
    return Settings(
        telegram_bot_token="test-token",
        model=DEFAULT_MODEL,
        model_api_key="not-a-real-key",
        database_url="sqlite+aiosqlite://",
    )


async def test_agent_model_call_survives_a_proxyless_install():
    # The knobs build_agent puts on the Agent's model — the SDK passes these
    # straight through to litellm.acompletion, so they are the fix under test.
    agent = build_agent(_settings())
    extra_args = dict(agent.model_settings.extra_args or {})

    try:
        await litellm.acompletion(
            model=DEFAULT_MODEL,
            messages=[{"role": "user", "content": "ping"}],
            tools=_TOOLS,
            api_key="not-a-real-key",
            api_base="http://127.0.0.1:9",  # nothing listens; fail fast, no real network
            timeout=5,
            **extra_args,
        )
    except Exception as exc:
        # A network/auth error means the call reached the wire — fine here.
        # A missing module means the Agent still pulls in the proxy chain.
        message = str(exc)
        assert "No module named" not in message, (
            f"Agent's model call pulls in an uninstalled proxy dependency: {message}"
        )
