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
from agentg.linking import (
    COACH_PROMOTED_INSTRUCTION,
    COACH_WELCOME_INSTRUCTION,
    CODE_NOT_FOUND_INSTRUCTION,
    DEAD_END_INSTRUCTION,
    Linking,
)
from agentg.runtime import AgentRuntime
from agentg.stores import Stores
from conftest import identity_phraser


async def null_summarizer(old_items, existing_notes):
    raise AssertionError("compaction should not trigger in this test")


@pytest.fixture
async def runtime(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'app.db'}")
    stores = Stores.from_engine(engine)
    runtime = AgentRuntime(
        agent=object(),
        engine=engine,
        stores=stores,
        linking=Linking(stores.linking, identity_phraser),
        summarizer=null_summarizer,
        stream_replies=False,
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
    gym = await runtime.stores.linking.create_gym("Iron Temple")

    ask = await runtime.handle_message(incoming("/start x", link_code=gym.invite_code))
    assert "Iron Temple" in ask and "Ana García" in ask
    assert await member_count(runtime.stores.linking) == 0  # not before the name is confirmed

    greet = await runtime.handle_message(incoming("yes"))
    assert "Ana García" in greet and "Iron Temple" in greet

    linked = await runtime.stores.linking.identity_for("telegram", "42")
    assert linked is not None
    assert linked.gym.id == gym.id and linked.member.name == "Ana García"


async def test_correcting_the_prefilled_name_uses_the_typed_name(runtime):
    gym = await runtime.stores.linking.create_gym("Iron Temple")
    await runtime.handle_message(incoming("/start x", link_code=gym.invite_code))

    greet = await runtime.handle_message(incoming("Call me Anita"))

    assert "Call me Anita" in greet
    linked = await runtime.stores.linking.identity_for("telegram", "42")
    assert linked is not None and linked.member.name == "Call me Anita"


async def test_a_deflecting_reply_is_re_asked_instead_of_becoming_the_name(runtime):
    gym = await runtime.stores.linking.create_gym("Iron Temple")
    await runtime.handle_message(incoming("/start x", link_code=gym.invite_code))

    ask = await runtime.handle_message(incoming("Ponme el nombre que tu quieras"))

    assert "Ponme el nombre que tu quieras" not in ask
    assert "Iron Temple" in ask  # still re-asking, not a dead end
    assert await member_count(runtime.stores.linking) == 0

    greet = await runtime.handle_message(incoming("Anita"))
    linked = await runtime.stores.linking.identity_for("telegram", "42")
    assert linked is not None and linked.member.name == "Anita"
    assert "Anita" in greet


async def test_missing_profile_name_is_asked_for_instead_of_confirmed(runtime):
    gym = await runtime.stores.linking.create_gym("Iron Temple")

    ask = await runtime.handle_message(
        incoming("/start x", display_name="", link_code=gym.invite_code)
    )
    assert "Iron Temple" in ask

    greet = await runtime.handle_message(incoming("Ana", display_name=""))
    linked = await runtime.stores.linking.identity_for("telegram", "42")
    assert linked is not None and linked.member.name == "Ana"
    assert "Ana" in greet


async def test_declining_the_prefilled_name_asks_for_one_instead(runtime):
    gym = await runtime.stores.linking.create_gym("Iron Temple")
    await runtime.handle_message(incoming("/start x", link_code=gym.invite_code))

    ask = await runtime.handle_message(incoming("no"))
    assert "no" not in ask.split()  # "no" must not become the name
    assert await member_count(runtime.stores.linking) == 0

    greet = await runtime.handle_message(incoming("Anita"))
    linked = await runtime.stores.linking.identity_for("telegram", "42")
    assert linked is not None and linked.member.name == "Anita"
    assert "Anita" in greet


async def test_pasting_a_code_mid_name_flow_restarts_linking(runtime):
    gym_a = await runtime.stores.linking.create_gym("Iron Temple")
    gym_b = await runtime.stores.linking.create_gym("Steel Yard")
    await runtime.handle_message(incoming("/start x", link_code=gym_a.invite_code))

    ask = await runtime.handle_message(incoming(gym_b.invite_code))
    assert "Steel Yard" in ask  # linking restarted, code not taken as a name

    await runtime.handle_message(incoming("yes"))
    linked = await runtime.stores.linking.identity_for("telegram", "42")
    assert linked is not None and linked.gym.id == gym_b.id
    assert linked.member.name == "Ana García"


async def test_regenerating_the_code_invalidates_a_pending_name_confirm(runtime):
    gym = await runtime.stores.linking.create_gym("Iron Temple")
    await runtime.handle_message(incoming("/start x", link_code=gym.invite_code))
    await runtime.stores.linking.regenerate_invite_code(gym.id)

    reply = await runtime.handle_message(incoming("yes"))

    assert await member_count(runtime.stores.linking) == 0
    assert "Iron Temple" not in reply  # expired invite, and gyms are not named


async def test_regenerating_the_code_invalidates_a_pending_switch(runtime):
    old_gym = await runtime.stores.linking.create_gym("Iron Temple")
    new_gym = await runtime.stores.linking.create_gym("Steel Yard")
    old_member = await runtime.stores.linking.link_member(old_gym.id, "Ana", "telegram", "42")
    await runtime.handle_message(incoming("/start x", link_code=new_gym.invite_code))
    await runtime.stores.linking.regenerate_invite_code(new_gym.id)

    reply = await runtime.handle_message(incoming("yes"))

    # Expired-code recovery response — no gym named in the expired reply.
    assert "Iron Temple" not in reply
    linked = await runtime.stores.linking.identity_for("telegram", "42")
    assert linked is not None and linked.member.id == old_member.id
    assert await member_count(runtime.stores.linking) == 1


# --- AC: the same code typed as plain text links too ---


async def test_invite_code_typed_as_plain_text_links(runtime):
    gym = await runtime.stores.linking.create_gym("Iron Temple")

    ask = await runtime.handle_message(incoming(f"  {gym.invite_code.upper()} "))
    assert "Iron Temple" in ask

    await runtime.handle_message(incoming("yes"))
    linked = await runtime.stores.linking.identity_for("telegram", "42")
    assert linked is not None and linked.gym.id == gym.id


# --- AC: the phraser sees what the person actually said, not just a template ---


async def test_the_phraser_receives_what_the_member_said(runtime):
    seen = []

    async def recording_phraser(instruction, member_text):
        seen.append((instruction, member_text))
        return instruction

    runtime.linking.phraser = recording_phraser
    gym = await runtime.stores.linking.create_gym("Iron Temple")

    await runtime.handle_message(incoming("/start x", link_code=gym.invite_code))
    await runtime.handle_message(incoming("No sé cómo podría llamarme"))

    assert seen[-1][1] == "No sé cómo podría llamarme"
    assert "Iron Temple" in seen[-1][0]


# --- AC: no or invalid code -> polite dead end, no Member row, no gym list ---


async def test_cold_start_without_a_code_is_a_polite_dead_end(runtime):
    await runtime.stores.linking.create_gym("Iron Temple")

    reply = await runtime.handle_message(incoming("/start", link_code=""))

    assert reply == DEAD_END_INSTRUCTION
    assert "Iron Temple" not in reply  # gyms are never listed
    assert await member_count(runtime.stores.linking) == 0


async def test_invalid_code_is_a_polite_dead_end(runtime):
    await runtime.stores.linking.create_gym("Iron Temple")

    for message in (incoming("/start x", link_code="wrong-code"), incoming("hello there")):
        reply = await runtime.handle_message(message)
        assert reply == DEAD_END_INSTRUCTION
        assert "Iron Temple" not in reply
    assert await member_count(runtime.stores.linking) == 0


# --- AC: a mistyped invite code is told so, not handed the generic dead end ---


async def test_a_near_miss_invite_code_is_told_the_code_did_not_work(runtime):
    await runtime.stores.linking.create_gym("Iron Temple")

    reply = await runtime.handle_message(incoming("8lrf8m6ee"))  # one char too many

    assert reply == CODE_NOT_FOUND_INSTRUCTION
    assert reply != DEAD_END_INSTRUCTION
    assert "Iron Temple" not in reply  # gyms are never listed
    assert await member_count(runtime.stores.linking) == 0


async def test_a_near_miss_coach_code_is_told_the_code_did_not_work(runtime):
    await runtime.stores.linking.create_gym("Iron Temple")

    reply = await runtime.handle_message(incoming("coach-8lrf8m6"))  # one char short

    assert reply == CODE_NOT_FOUND_INSTRUCTION
    assert await member_count(runtime.stores.linking) == 0


async def test_a_near_miss_code_arriving_as_a_deep_link_is_also_told(runtime):
    await runtime.stores.linking.create_gym("Iron Temple")

    reply = await runtime.handle_message(
        incoming("/start 8lrf8m6ee", link_code="8lrf8m6ee")
    )

    assert reply == CODE_NOT_FOUND_INSTRUCTION
    assert await member_count(runtime.stores.linking) == 0


@pytest.mark.parametrize(
    "text",
    [
        "Hola",  # a greeting
        "gracias",  # a short courtesy
        "perfecto",  # 8 alphabet chars — code length, but an ordinary word
        "quiero entrenar",  # a plain request
        "mi codigo no funciona",  # talking *about* a code, not typing one
    ],
)
async def test_ordinary_short_messages_still_get_the_generic_dead_end(runtime, text):
    await runtime.stores.linking.create_gym("Iron Temple")

    reply = await runtime.handle_message(incoming(text))

    assert reply == DEAD_END_INSTRUCTION
    assert await member_count(runtime.stores.linking) == 0


# --- AC: same-gym re-link -> plain greeting, no duplicates ---


async def test_same_gym_relink_greets_without_duplicating(runtime):
    gym = await runtime.stores.linking.create_gym("Iron Temple")
    await runtime.stores.linking.link_member(gym.id, "Ana", "telegram", "42")

    reply = await runtime.handle_message(incoming("/start x", link_code=gym.invite_code))

    assert "Iron Temple" in reply and "Ana" in reply
    assert await member_count(runtime.stores.linking) == 1


# --- AC: different-gym link -> explicit confirm, fresh start, re-point ---


async def test_switching_gyms_requires_an_explicit_confirm(runtime):
    old_gym = await runtime.stores.linking.create_gym("Iron Temple")
    new_gym = await runtime.stores.linking.create_gym("Steel Yard")
    old_member = await runtime.stores.linking.link_member(old_gym.id, "Ana", "telegram", "42")

    confirm = await runtime.handle_message(incoming("/start x", link_code=new_gym.invite_code))
    assert "Steel Yard" in confirm and "Iron Temple" in confirm  # history stays with old gym
    assert await member_count(runtime.stores.linking) == 1  # nothing switched yet

    done = await runtime.handle_message(incoming("yes"))
    assert "Steel Yard" in done

    linked = await runtime.stores.linking.identity_for("telegram", "42")
    assert linked is not None
    assert linked.gym.id == new_gym.id
    assert linked.member.id != old_member.id
    assert linked.member.name == "Ana"  # same person, fresh row
    assert await member_count(runtime.stores.linking) == 2  # old Member row untouched


async def test_declining_the_switch_keeps_the_old_gym(runtime):
    old_gym = await runtime.stores.linking.create_gym("Iron Temple")
    new_gym = await runtime.stores.linking.create_gym("Steel Yard")
    old_member = await runtime.stores.linking.link_member(old_gym.id, "Ana", "telegram", "42")

    await runtime.handle_message(incoming("/start x", link_code=new_gym.invite_code))
    reply = await runtime.handle_message(incoming("no"))

    assert "Iron Temple" in reply
    linked = await runtime.stores.linking.identity_for("telegram", "42")
    assert linked is not None and linked.member.id == old_member.id
    assert await member_count(runtime.stores.linking) == 1


# --- AC: conversation history is keyed by member id ---


async def test_linked_chat_runs_the_agent_with_a_member_keyed_session(runtime, monkeypatch):
    seen = {}

    async def fake_run(agent, text, *, session, context=None, run_config=None):
        seen["text"], seen["session_id"] = text, session.session_id
        return SimpleNamespace(final_output="nice!")

    monkeypatch.setattr(runtime_module.Runner, "run", fake_run)
    gym = await runtime.stores.linking.create_gym("Iron Temple")
    member = await runtime.stores.linking.link_member(gym.id, "Ana", "telegram", "42")

    reply = await runtime.handle_message(incoming("bench 60 8,8,8"))

    assert reply == "nice!"
    assert seen["text"] == "bench 60 8,8,8"
    assert seen["session_id"] == f"member:{member.id}"


async def test_switching_gyms_leaves_the_old_history_behind(runtime, monkeypatch):
    sessions_seen = []

    async def fake_run(agent, text, *, session, context=None, run_config=None):
        sessions_seen.append(session.session_id)
        return SimpleNamespace(final_output="ok")

    monkeypatch.setattr(runtime_module.Runner, "run", fake_run)
    old_gym = await runtime.stores.linking.create_gym("Iron Temple")
    new_gym = await runtime.stores.linking.create_gym("Steel Yard")
    await runtime.stores.linking.link_member(old_gym.id, "Ana", "telegram", "42")

    await runtime.handle_message(incoming("hey"))
    await runtime.handle_message(incoming("/start x", link_code=new_gym.invite_code))
    await runtime.handle_message(incoming("yes"))
    await runtime.handle_message(incoming("hey again"))

    assert len(sessions_seen) == 2
    assert sessions_seen[0] != sessions_seen[1]  # fresh history at the new Gym


# --- linked members keep chatting normally ---


async def test_linked_member_messages_go_to_the_agent(runtime, monkeypatch):
    async def fake_run(agent, text, *, session, context=None, run_config=None):
        return SimpleNamespace(final_output="let's go!")

    monkeypatch.setattr(runtime_module.Runner, "run", fake_run)
    gym = await runtime.stores.linking.create_gym("Iron Temple")
    await runtime.stores.linking.link_member(gym.id, "Ana", "telegram", "42")

    assert await runtime.handle_message(incoming("I'm here")) == "let's go!"


async def test_linked_member_tapping_an_inactive_link_stays_linked(runtime):
    gym = await runtime.stores.linking.create_gym("Iron Temple")
    await runtime.stores.linking.link_member(gym.id, "Ana", "telegram", "42")

    reply = await runtime.handle_message(incoming("/start x", link_code="stale-code"))

    assert "Iron Temple" in reply  # reassured, not dead-ended
    linked = await runtime.stores.linking.identity_for("telegram", "42")
    assert linked is not None and linked.gym.id == gym.id


# --- AC: the coach invite link (issue #104) ---


async def test_coach_deep_link_links_a_newcomer_coach_flagged(runtime):
    gym = await runtime.stores.linking.create_gym("Iron Temple")

    ask = await runtime.handle_message(incoming("/start x", link_code=gym.coach_invite_code))
    assert "Iron Temple" in ask and "Ana García" in ask
    assert await member_count(runtime.stores.linking) == 0  # name confirmed first

    greet = await runtime.handle_message(incoming("yes"))

    assert greet == COACH_WELCOME_INSTRUCTION.format(name="Ana García", gym="Iron Temple")
    linked = await runtime.stores.linking.identity_for("telegram", "42")
    assert linked is not None
    assert linked.gym.id == gym.id and linked.member.is_coach is True


async def test_the_coach_welcome_does_not_start_intake(runtime):
    gym = await runtime.stores.linking.create_gym("Iron Temple")
    await runtime.handle_message(incoming("/start x", link_code=gym.coach_invite_code))

    instruction = await runtime.handle_message(incoming("yes"))

    # coach-aware: the rules doc, routines, /dashboard — and Intake waits for
    # an explicit ask instead of starting
    assert "/dashboard" in instruction
    assert "intake" in instruction.lower()


async def test_coach_code_typed_as_plain_text_links_prefix_included(runtime):
    gym = await runtime.stores.linking.create_gym("Iron Temple")

    ask = await runtime.handle_message(incoming(f"  {gym.coach_invite_code.upper()} "))
    assert "Iron Temple" in ask

    await runtime.handle_message(incoming("yes"))
    linked = await runtime.stores.linking.identity_for("telegram", "42")
    assert linked is not None
    assert linked.gym.id == gym.id and linked.member.is_coach is True


async def test_an_existing_member_is_promoted_in_place(runtime):
    gym = await runtime.stores.linking.create_gym("Iron Temple")
    member = await runtime.stores.linking.link_member(gym.id, "Ana", "telegram", "42")

    reply = await runtime.handle_message(incoming("/start x", link_code=gym.coach_invite_code))

    assert reply == COACH_PROMOTED_INSTRUCTION.format(name="Ana", gym="Iron Temple")
    assert await member_count(runtime.stores.linking) == 1  # no new Member row
    linked = await runtime.stores.linking.identity_for("telegram", "42")
    assert linked is not None
    assert linked.member.id == member.id and linked.member.is_coach is True


async def test_an_existing_coach_retapping_the_link_is_reassured(runtime):
    gym = await runtime.stores.linking.create_gym("Iron Temple")
    member = await runtime.stores.linking.link_member(gym.id, "Ana", "telegram", "42")
    await runtime.stores.linking.set_coach(member.id)

    reply = await runtime.handle_message(incoming("/start x", link_code=gym.coach_invite_code))

    assert "Iron Temple" in reply and "Ana" in reply
    assert await member_count(runtime.stores.linking) == 1
    linked = await runtime.stores.linking.identity_for("telegram", "42")
    assert linked is not None and linked.member.is_coach is True


async def test_another_gyms_coach_link_is_the_normal_switch_arriving_coach_flagged(runtime):
    old_gym = await runtime.stores.linking.create_gym("Iron Temple")
    new_gym = await runtime.stores.linking.create_gym("Steel Yard")
    old_member = await runtime.stores.linking.link_member(old_gym.id, "Ana", "telegram", "42")

    confirm = await runtime.handle_message(incoming("/start x", link_code=new_gym.coach_invite_code))
    assert "Steel Yard" in confirm and "Iron Temple" in confirm
    assert await member_count(runtime.stores.linking) == 1  # nothing switched yet

    await runtime.handle_message(incoming("yes"))

    linked = await runtime.stores.linking.identity_for("telegram", "42")
    assert linked is not None
    assert linked.gym.id == new_gym.id
    assert linked.member.id != old_member.id and linked.member.is_coach is True
    assert await member_count(runtime.stores.linking) == 2  # old Member row untouched


async def test_declining_a_coach_switch_keeps_the_old_gym_unflagged(runtime):
    old_gym = await runtime.stores.linking.create_gym("Iron Temple")
    new_gym = await runtime.stores.linking.create_gym("Steel Yard")
    old_member = await runtime.stores.linking.link_member(old_gym.id, "Ana", "telegram", "42")

    await runtime.handle_message(incoming("/start x", link_code=new_gym.coach_invite_code))
    await runtime.handle_message(incoming("no"))

    linked = await runtime.stores.linking.identity_for("telegram", "42")
    assert linked is not None
    assert linked.member.id == old_member.id and linked.member.is_coach is False


async def test_regenerating_the_coach_code_invalidates_a_pending_name_confirm(runtime):
    gym = await runtime.stores.linking.create_gym("Iron Temple")
    await runtime.handle_message(incoming("/start x", link_code=gym.coach_invite_code))
    await runtime.stores.linking.regenerate_coach_invite_code(gym.id)

    reply = await runtime.handle_message(incoming("yes"))

    assert await member_count(runtime.stores.linking) == 0
    assert "Iron Temple" not in reply  # expired invite, and gyms are not named


async def test_regenerating_the_coach_code_invalidates_a_pending_switch(runtime):
    old_gym = await runtime.stores.linking.create_gym("Iron Temple")
    new_gym = await runtime.stores.linking.create_gym("Steel Yard")
    old_member = await runtime.stores.linking.link_member(old_gym.id, "Ana", "telegram", "42")
    await runtime.handle_message(incoming("/start x", link_code=new_gym.coach_invite_code))
    await runtime.stores.linking.regenerate_coach_invite_code(new_gym.id)

    reply = await runtime.handle_message(incoming("yes"))

    # Expired-code recovery response — no gym named in the expired reply.
    assert "Iron Temple" not in reply
    linked = await runtime.stores.linking.identity_for("telegram", "42")
    assert linked is not None
    assert linked.member.id == old_member.id and linked.member.is_coach is False
    assert await member_count(runtime.stores.linking) == 1


async def test_pasting_a_coach_code_mid_name_flow_restarts_linking(runtime):
    gym_a = await runtime.stores.linking.create_gym("Iron Temple")
    gym_b = await runtime.stores.linking.create_gym("Steel Yard")
    await runtime.handle_message(incoming("/start x", link_code=gym_a.invite_code))

    ask = await runtime.handle_message(incoming(gym_b.coach_invite_code))
    assert "Steel Yard" in ask  # linking restarted, code not taken as a name

    await runtime.handle_message(incoming("yes"))
    linked = await runtime.stores.linking.identity_for("telegram", "42")
    assert linked is not None
    assert linked.gym.id == gym_b.id and linked.member.is_coach is True


# --- AC: a message that cannot be an Invite code performs no Invite code lookups (#169) ---


async def test_an_ordinary_message_skips_invite_code_lookups(runtime, monkeypatch):
    """A message like 'hola' can't be a code — skip the DB lookups entirely."""
    calls: list[str] = []

    original_gym_by_code = runtime.stores.linking.gym_by_invite_code
    original_gym_by_coach = runtime.stores.linking.gym_by_coach_invite_code

    async def spy_gym_by_code(text: str):
        calls.append("invite")
        return await original_gym_by_code(text)

    async def spy_gym_by_coach(text: str):
        calls.append("coach")
        return await original_gym_by_coach(text)

    runtime.stores.linking.gym_by_invite_code = spy_gym_by_code  # type: ignore[method-assign]
    runtime.stores.linking.gym_by_coach_invite_code = spy_gym_by_coach  # type: ignore[method-assign]

    gym = await runtime.stores.linking.create_gym("Iron Temple")

    # An unlinked user sending a greeting: no code lookups
    calls.clear()
    reply = await runtime.handle_message(incoming("hola"))
    assert reply == DEAD_END_INSTRUCTION
    assert calls == []

    # A linked member sending a normal message skips lookups too
    async def fake_run(agent, text, *, session, context=None, run_config=None):
        return SimpleNamespace(final_output="ok")

    monkeypatch.setattr(runtime_module.Runner, "run", fake_run)
    await runtime.stores.linking.link_member(gym.id, "Ana", "telegram", "42")

    calls.clear()
    reply = await runtime.handle_message(incoming("I'm here today"))
    assert reply == "ok"
    assert calls == []


async def test_typing_a_real_invite_code_still_does_the_lookup(runtime):
    """AC: Typing a real Invite or Coach code still links, exactly as today."""
    calls: list[str] = []

    original_gym_by_code = runtime.stores.linking.gym_by_invite_code
    original_gym_by_coach = runtime.stores.linking.gym_by_coach_invite_code

    async def spy_gym_by_code(text: str):
        calls.append("invite")
        return await original_gym_by_code(text)

    async def spy_gym_by_coach(text: str):
        calls.append("coach")
        return await original_gym_by_coach(text)

    runtime.stores.linking.gym_by_invite_code = spy_gym_by_code  # type: ignore[method-assign]
    runtime.stores.linking.gym_by_coach_invite_code = spy_gym_by_coach  # type: ignore[method-assign]

    gym = await runtime.stores.linking.create_gym("Iron Temple")

    # Typing a real invite code triggers the lookup and links
    calls.clear()
    reply = await runtime.handle_message(incoming(f"  {gym.invite_code.upper()} "))
    assert "Iron Temple" in reply
    assert calls == ["invite"]  # one lookup, found it — no need for the coach lookup

    # Typing (only) a real coach code also triggers the lookup
    calls.clear()
    reply = await runtime.handle_message(incoming(f"{gym.coach_invite_code}"))
    assert "Iron Temple" in reply
    assert calls == ["invite", "coach"]  # both lookups — invite miss, coach hit
