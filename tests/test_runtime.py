"""AgentRuntime: schema startup, member-keyed history, serialized turns."""

import asyncio
from types import SimpleNamespace

import pytest

import agentg.runtime as runtime_module
from agentg.db import create_engine
from agentg.messages import IncomingMessage
from agentg.linking import Linking
from agentg.linking_store import LinkedIdentity
from agentg.routines import ExerciseSpec, WorkoutSpec
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


# --- member_context gating flags (issue #174) ---


def bench_spec():
    return [WorkoutSpec(weekday=0, name="Push", exercises=[ExerciseSpec("bench press", sets=3, reps="5")])]


async def test_member_context_can_author_routine_no_routine(runtime):
    """A new Member with no routine: can_author_routine is True."""
    gym = await runtime.stores.linking.create_gym("Iron Temple")
    member = await runtime.stores.linking.link_member(gym.id, "Ana", "telegram", "1")
    linked = LinkedIdentity(member=member, gym=gym)

    ctx = await runtime.member_context(linked)
    assert ctx.can_author_routine is True


async def test_member_context_can_author_routine_after_agent_routine(runtime):
    """A Member with an agent-generated routine: can_author_routine is True
    (the Agent can restructure it on request)."""
    gym = await runtime.stores.linking.create_gym("Iron Temple")
    member = await runtime.stores.linking.link_member(gym.id, "Ana", "telegram", "1")
    # An agent-generated routine (coach_authored omitted / defaults to False).
    await runtime.stores.routines.save_routine(member.id, gym.id, bench_spec())
    linked = LinkedIdentity(member=member, gym=gym)

    ctx = await runtime.member_context(linked)
    assert ctx.can_author_routine is True


async def test_member_context_can_author_routine_after_coach_routine(runtime):
    """A Member with a coach-authored routine: can_author_routine is False
    (the Agent never restructures coach-authored routines)."""
    gym = await runtime.stores.linking.create_gym("Iron Temple")
    member = await runtime.stores.linking.link_member(gym.id, "Ana", "telegram", "1")
    await runtime.stores.routines.save_routine(
        member.id, gym.id, bench_spec(), coach_authored=True
    )
    linked = LinkedIdentity(member=member, gym=gym)

    ctx = await runtime.member_context(linked)
    assert ctx.can_author_routine is False


async def test_member_context_can_author_routine_for_coach_with_own_routine(runtime):
    """A Coach always gets can_author_routine=True, even with their own
    coach-authored routine (is_coach dominates)."""
    gym = await runtime.stores.linking.create_gym("Iron Temple")
    coach = await runtime.stores.linking.link_member(gym.id, "Coach Sam", "telegram", "2")
    await runtime.stores.linking.set_coach(coach.id)
    await runtime.stores.routines.save_routine(
        coach.id, gym.id, bench_spec(), coach_authored=True
    )
    # Re-fetch to get the fresh is_coach flag from the DB (set_coach writes
    # through SQL without refreshing the in-memory model).
    linked = await runtime.stores.linking.identity_for("telegram", "2")

    ctx = await runtime.member_context(linked)
    assert ctx.can_author_routine is True
