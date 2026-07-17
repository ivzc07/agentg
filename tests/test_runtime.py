"""AgentRuntime: schema startup, member-keyed history, serialized turns."""

import asyncio
from types import SimpleNamespace

import pytest

import agentg.runtime as runtime_module
from agentg.db import create_engine
from agentg.messages import IncomingMessage
from agentg.onboarding import Onboarding
from agentg.runtime import AgentRuntime
from agentg.store import LinkingStore
from agentg.training import TrainingStore


def sqlite_url(tmp_path) -> str:
    return f"sqlite+aiosqlite:///{tmp_path / 'memory.db'}"


def make_runtime(url) -> AgentRuntime:
    engine = create_engine(url)
    store = LinkingStore(engine)
    return AgentRuntime(
        agent=object(),
        engine=engine,
        store=store,
        onboarding=Onboarding(store),
        training=TrainingStore(engine),
    )


def incoming(text, user_id):
    return IncomingMessage(
        channel="telegram", channel_user_id=user_id, text=text, display_name="Ana"
    )


@pytest.fixture
async def runtime(tmp_path):
    runtime = make_runtime(sqlite_url(tmp_path))
    await runtime.ensure_schema()
    yield runtime
    await runtime.engine.dispose()


async def test_history_survives_a_process_restart(tmp_path):
    url = sqlite_url(tmp_path)
    turn = [{"role": "user", "content": "bench was 60 today"}]

    runtime = make_runtime(url)
    await runtime.ensure_schema()
    await runtime.session_for_member(1).add_items(turn)
    await runtime.engine.dispose()  # the process dies

    runtime = make_runtime(url)  # ...and comes back
    await runtime.ensure_schema()
    assert await runtime.session_for_member(1).get_items() == turn
    await runtime.engine.dispose()


async def test_member_histories_are_isolated_from_each_other(runtime):
    await runtime.session_for_member(1).add_items([{"role": "user", "content": "my knee hurts"}])
    assert await runtime.session_for_member(2).get_items() == []


async def test_turns_in_one_conversation_never_interleave(runtime, monkeypatch):
    running: set[str] = set()
    overlapped = []

    async def fake_run(agent, text, *, session, context=None):
        if session.session_id in running:
            overlapped.append(text)
        running.add(session.session_id)
        await asyncio.sleep(0.01)
        running.discard(session.session_id)
        return SimpleNamespace(final_output="ok")

    monkeypatch.setattr(runtime_module.Runner, "run", fake_run)
    gym = await runtime.store.create_gym("Iron Temple")
    await runtime.store.link_member(gym.id, "Ana", "telegram", "42")
    await runtime.store.link_member(gym.id, "Ben", "telegram", "7")

    await asyncio.gather(
        runtime.handle_message(incoming("first", "42")),
        runtime.handle_message(incoming("second", "42")),
        runtime.handle_message(incoming("other member", "7")),
    )

    assert overlapped == []
