"""AgentRuntime: schema startup, member-keyed history, serialized turns."""

import asyncio
from types import SimpleNamespace

import pytest

import agentg.runtime as runtime_module
from agentg.db import create_engine
from agentg.messages import IncomingMessage
from agentg.linking import Linking
from agentg.runtime import AgentRuntime
from agentg.stores import Stores
from conftest import unused_phraser


async def null_summarizer(old_items, existing_notes):
    raise AssertionError("compaction should not trigger in this test")


def sqlite_url(tmp_path) -> str:
    return f"sqlite+aiosqlite:///{tmp_path / 'memory.db'}"


def make_runtime(url) -> AgentRuntime:
    engine = create_engine(url)
    stores = Stores.from_engine(engine)
    return AgentRuntime(
        agent=object(),
        engine=engine,
        stores=stores,
        linking=Linking(stores.linking, unused_phraser),
        summarizer=null_summarizer,
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
    gym = await runtime.stores.linking.create_gym("Iron Temple")
    await runtime.stores.linking.link_member(gym.id, "Ana", "telegram", "42")
    await runtime.stores.linking.link_member(gym.id, "Ben", "telegram", "7")

    await asyncio.gather(
        runtime.handle_message(incoming("first", "42")),
        runtime.handle_message(incoming("second", "42")),
        runtime.handle_message(incoming("other member", "7")),
    )

    assert overlapped == []


# --- AC: the rhythm reset no longer blocks the reply, and lapsed Members are still revived (#169) ---


async def test_reset_rhythm_is_deferred_past_the_reply(runtime, monkeypatch):
    """reset_rhythm must not block the LLM call — it fires after_send."""
    events: list[str] = []

    async def fake_run(agent, text, *, session, context=None):
        events.append("llm")
        return SimpleNamespace(final_output="ok")

    monkeypatch.setattr(runtime_module.Runner, "run", fake_run)

    original_reset = runtime.stores.checkins.reset_rhythm

    async def spy_reset(member_id: int) -> None:
        events.append("reset_rhythm")
        await original_reset(member_id)

    runtime.stores.checkins.reset_rhythm = spy_reset  # type: ignore[method-assign]

    gym = await runtime.stores.linking.create_gym("Iron Temple")
    await runtime.stores.linking.link_member(gym.id, "Ana", "telegram", "42")

    reply = await runtime.handle_message(incoming("I'm here", "42"))

    # The LLM ran before the reply was complete; reset_rhythm was only queued.
    assert "llm" in events
    # The reset_rhythm hasn't fired yet — it's deferred to after_send.
    assert "reset_rhythm" not in events

    # Now await after_send to simulate the channel adapter's delivery.
    if reply.after_send is not None:
        await reply.after_send()

    # After delivery, reset_rhythm fires.
    assert events.index("llm") < events.index("reset_rhythm")


async def test_deferred_reset_rhythm_still_revives_lapsed_members(runtime, monkeypatch):
    """A lapsed Member is revived after the reply, not before."""
    async def fake_run(agent, text, *, session, context=None):
        return SimpleNamespace(final_output="welcome back!")

    monkeypatch.setattr(runtime_module.Runner, "run", fake_run)

    gym = await runtime.stores.linking.create_gym("Iron Temple")
    await runtime.stores.linking.link_member(gym.id, "Ana", "telegram", "42")
    await runtime.stores.checkins.lapse(1)  # member id 1

    state_before, _ = await runtime.stores.checkins.get_state(1)
    assert state_before == "lapsed"

    reply = await runtime.handle_message(incoming("I'm back", "42"))
    assert reply == "welcome back!"

    # The lapsed state is still visible during the reply (reset not yet applied).
    # After after_send, the Member is revived.
    if reply.after_send is not None:
        await reply.after_send()

    state_after, _ = await runtime.stores.checkins.get_state(1)
    assert state_after == "on"
