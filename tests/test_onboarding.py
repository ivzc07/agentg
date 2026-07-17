"""Gym linking end to end through the runtime (spec §Onboarding & gym linking).

Each test drives ``AgentRuntime.handle_message`` the way a channel adapter
would; ``Runner.run`` is monkeypatched so the LLM never runs. Linking itself
is deterministic — the Agent only speaks for linked Members.
"""

from types import SimpleNamespace

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

import agentg.runtime as runtime_module
from agentg.db import create_engine
from agentg.messages import IncomingMessage
from agentg.models import Member
from agentg.onboarding import DEAD_END, Onboarding
from agentg.runtime import AgentRuntime
from agentg.store import LinkingStore


@pytest.fixture
async def runtime(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'app.db'}")
    store = LinkingStore(engine)
    runtime = AgentRuntime(
        agent=object(), engine=engine, store=store, onboarding=Onboarding(store)
    )
    await runtime.ensure_schema()
    yield runtime
    await engine.dispose()


def incoming(text="hi", *, user_id="42", display_name="Ana García", link_code=None):
    return IncomingMessage(
        channel="telegram",
        channel_user_id=user_id,
        text=text,
        display_name=display_name,
        link_code=link_code,
    )


async def member_count(store):
    async with async_sessionmaker(store.engine)() as db:
        return await db.scalar(select(func.count()).select_from(Member))


# --- AC: valid deep link creates the Member, confirms the name, greets ---


async def test_deep_link_confirms_the_profile_name_then_creates_and_greets(runtime):
    gym = await runtime.store.create_gym("Iron Temple")

    ask = await runtime.handle_message(incoming("/start x", link_code=gym.invite_code))
    assert "Iron Temple" in ask and "Ana García" in ask
    assert await member_count(runtime.store) == 0  # not before the name is confirmed

    greet = await runtime.handle_message(incoming("yes"))
    assert "Ana García" in greet and "Iron Temple" in greet

    linked = await runtime.store.identity_for("telegram", "42")
    assert linked is not None
    assert linked.gym.id == gym.id and linked.member.name == "Ana García"


async def test_correcting_the_prefilled_name_uses_the_typed_name(runtime):
    gym = await runtime.store.create_gym("Iron Temple")
    await runtime.handle_message(incoming("/start x", link_code=gym.invite_code))

    greet = await runtime.handle_message(incoming("Call me Anita"))

    assert "Call me Anita" in greet
    linked = await runtime.store.identity_for("telegram", "42")
    assert linked is not None and linked.member.name == "Call me Anita"


async def test_missing_profile_name_is_asked_for_instead_of_confirmed(runtime):
    gym = await runtime.store.create_gym("Iron Temple")

    ask = await runtime.handle_message(
        incoming("/start x", display_name="", link_code=gym.invite_code)
    )
    assert "Iron Temple" in ask

    greet = await runtime.handle_message(incoming("Ana", display_name=""))
    linked = await runtime.store.identity_for("telegram", "42")
    assert linked is not None and linked.member.name == "Ana"
    assert "Ana" in greet


# --- AC: the same code typed as plain text links too ---


async def test_invite_code_typed_as_plain_text_links(runtime):
    gym = await runtime.store.create_gym("Iron Temple")

    ask = await runtime.handle_message(incoming(f"  {gym.invite_code.upper()} "))
    assert "Iron Temple" in ask

    await runtime.handle_message(incoming("yes"))
    linked = await runtime.store.identity_for("telegram", "42")
    assert linked is not None and linked.gym.id == gym.id


# --- AC: no or invalid code -> polite dead end, no Member row, no gym list ---


async def test_cold_start_without_a_code_is_a_polite_dead_end(runtime):
    await runtime.store.create_gym("Iron Temple")

    reply = await runtime.handle_message(incoming("/start", link_code=""))

    assert reply == DEAD_END
    assert "Iron Temple" not in reply  # gyms are never listed
    assert await member_count(runtime.store) == 0


async def test_invalid_code_is_a_polite_dead_end(runtime):
    await runtime.store.create_gym("Iron Temple")

    for message in (incoming("/start x", link_code="wrong-code"), incoming("hello there")):
        reply = await runtime.handle_message(message)
        assert reply == DEAD_END
        assert "Iron Temple" not in reply
    assert await member_count(runtime.store) == 0


# --- AC: same-gym re-link -> plain greeting, no duplicates ---


async def test_same_gym_relink_greets_without_duplicating(runtime):
    gym = await runtime.store.create_gym("Iron Temple")
    await runtime.store.link_member(gym.id, "Ana", "telegram", "42")

    reply = await runtime.handle_message(incoming("/start x", link_code=gym.invite_code))

    assert "Iron Temple" in reply and "Ana" in reply
    assert await member_count(runtime.store) == 1


# --- AC: different-gym link -> explicit confirm, fresh start, re-point ---


async def test_switching_gyms_requires_an_explicit_confirm(runtime):
    old_gym = await runtime.store.create_gym("Iron Temple")
    new_gym = await runtime.store.create_gym("Steel Yard")
    old_member = await runtime.store.link_member(old_gym.id, "Ana", "telegram", "42")

    confirm = await runtime.handle_message(incoming("/start x", link_code=new_gym.invite_code))
    assert "Steel Yard" in confirm and "Iron Temple" in confirm  # history stays with old gym
    assert await member_count(runtime.store) == 1  # nothing switched yet

    done = await runtime.handle_message(incoming("yes"))
    assert "Steel Yard" in done

    linked = await runtime.store.identity_for("telegram", "42")
    assert linked is not None
    assert linked.gym.id == new_gym.id
    assert linked.member.id != old_member.id
    assert linked.member.name == "Ana"  # same person, fresh row
    assert await member_count(runtime.store) == 2  # old Member row untouched


async def test_declining_the_switch_keeps_the_old_gym(runtime):
    old_gym = await runtime.store.create_gym("Iron Temple")
    new_gym = await runtime.store.create_gym("Steel Yard")
    old_member = await runtime.store.link_member(old_gym.id, "Ana", "telegram", "42")

    await runtime.handle_message(incoming("/start x", link_code=new_gym.invite_code))
    reply = await runtime.handle_message(incoming("no"))

    assert "Iron Temple" in reply
    linked = await runtime.store.identity_for("telegram", "42")
    assert linked is not None and linked.member.id == old_member.id
    assert await member_count(runtime.store) == 1


# --- AC: conversation history is keyed by member id ---


async def test_linked_chat_runs_the_agent_with_a_member_keyed_session(runtime, monkeypatch):
    seen = {}

    async def fake_run(agent, text, *, session):
        seen["text"], seen["session_id"] = text, session.session_id
        return SimpleNamespace(final_output="nice!")

    monkeypatch.setattr(runtime_module.Runner, "run", fake_run)
    gym = await runtime.store.create_gym("Iron Temple")
    member = await runtime.store.link_member(gym.id, "Ana", "telegram", "42")

    reply = await runtime.handle_message(incoming("bench 60 8,8,8"))

    assert reply == "nice!"
    assert seen["text"] == "bench 60 8,8,8"
    assert seen["session_id"] == f"member:{member.id}"


async def test_switching_gyms_leaves_the_old_history_behind(runtime, monkeypatch):
    sessions_seen = []

    async def fake_run(agent, text, *, session):
        sessions_seen.append(session.session_id)
        return SimpleNamespace(final_output="ok")

    monkeypatch.setattr(runtime_module.Runner, "run", fake_run)
    old_gym = await runtime.store.create_gym("Iron Temple")
    new_gym = await runtime.store.create_gym("Steel Yard")
    await runtime.store.link_member(old_gym.id, "Ana", "telegram", "42")

    await runtime.handle_message(incoming("hey"))
    await runtime.handle_message(incoming("/start x", link_code=new_gym.invite_code))
    await runtime.handle_message(incoming("yes"))
    await runtime.handle_message(incoming("hey again"))

    assert len(sessions_seen) == 2
    assert sessions_seen[0] != sessions_seen[1]  # fresh history at the new Gym


# --- linked members keep chatting normally ---


async def test_linked_member_messages_go_to_the_agent(runtime, monkeypatch):
    async def fake_run(agent, text, *, session):
        return SimpleNamespace(final_output="let's go!")

    monkeypatch.setattr(runtime_module.Runner, "run", fake_run)
    gym = await runtime.store.create_gym("Iron Temple")
    await runtime.store.link_member(gym.id, "Ana", "telegram", "42")

    assert await runtime.handle_message(incoming("I'm here")) == "let's go!"


async def test_linked_member_tapping_an_inactive_link_stays_linked(runtime):
    gym = await runtime.store.create_gym("Iron Temple")
    await runtime.store.link_member(gym.id, "Ana", "telegram", "42")

    reply = await runtime.handle_message(incoming("/start x", link_code="stale-code"))

    assert "Iron Temple" in reply  # reassured, not dead-ended
    linked = await runtime.store.identity_for("telegram", "42")
    assert linked is not None and linked.gym.id == gym.id
