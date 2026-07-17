"""AgentRuntime: conversation keys, agent invocation, history across restarts."""

from types import SimpleNamespace

import agentg.runtime as runtime_module
from agentg.db import create_engine
from agentg.runtime import AgentRuntime, conversation_key


def sqlite_url(tmp_path) -> str:
    return f"sqlite+aiosqlite:///{tmp_path / 'memory.db'}"


def test_conversation_key_is_channel_scoped():
    assert conversation_key("telegram", "42") == "telegram:42"


async def test_handle_message_runs_the_agent_against_the_conversation_session(
    tmp_path, monkeypatch
):
    seen = {}

    async def fake_run(agent, text, *, session):
        seen["agent"], seen["text"], seen["session"] = agent, text, session
        return SimpleNamespace(final_output="hey!")

    monkeypatch.setattr(runtime_module.Runner, "run", fake_run)
    engine = create_engine(sqlite_url(tmp_path))
    agent = object()
    runtime = AgentRuntime(agent=agent, engine=engine)

    reply = await runtime.handle_message("telegram", "42", "hi")

    assert reply == "hey!"
    assert seen["agent"] is agent
    assert seen["text"] == "hi"
    assert seen["session"].session_id == "telegram:42"
    await engine.dispose()


async def test_history_survives_a_process_restart(tmp_path):
    url = sqlite_url(tmp_path)
    turn = [{"role": "user", "content": "bench was 60 today"}]

    engine = create_engine(url)
    session = AgentRuntime(agent=object(), engine=engine).session_for("telegram", "42")
    await session.add_items(turn)
    await engine.dispose()  # the process dies

    engine = create_engine(url)  # ...and comes back
    session = AgentRuntime(agent=object(), engine=engine).session_for("telegram", "42")
    assert await session.get_items() == turn
    await engine.dispose()


async def test_conversations_are_isolated_from_each_other(tmp_path):
    engine = create_engine(sqlite_url(tmp_path))
    runtime = AgentRuntime(agent=object(), engine=engine)

    await runtime.session_for("telegram", "1").add_items(
        [{"role": "user", "content": "my knee hurts"}]
    )

    assert await runtime.session_for("telegram", "2").get_items() == []
    await engine.dispose()
