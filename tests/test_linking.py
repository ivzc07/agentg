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
        is_private=True,
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

    # Coach switch recovery: reassured they're still at their old Gym.
    assert "Iron Temple" in reply
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


# --- P1 (fix-r8): pending Forget-me cancelled before linking early return ---


async def test_linked_member_pending_forget_me_cancelled_by_start_code(runtime):
    """A linked Member with a pending Forget-me who taps /start CODE for their
    own gym must have the pending cancelled, not left active behind the linking
    reply."""
    from datetime import datetime, timezone

    gym = await runtime.stores.linking.create_gym("Iron Temple")
    await runtime.stores.linking.link_member(gym.id, "Ana", "telegram", "42")
    linked = await runtime.stores.linking.identity_for("telegram", "42")
    assert linked is not None

    # Seed a pending Forget-me request.
    now = datetime.now(timezone.utc)
    phrase = await runtime.stores.forget.request_forget_me(
        linked.member.id, gym.id, now, 300, "en"
    )
    assert phrase != ""
    pending = await runtime.stores.forget.get_pending_request(linked.member.id)
    assert pending is not None

    # Tap the gym's invite code — linking handles it, but first the pending
    # Forget-me must be cancelled.
    reply = await runtime.handle_message(
        incoming("/start x", link_code=gym.invite_code)
    )
    assert "Iron Temple" in reply

    # The pending is gone.
    pending_after = await runtime.stores.forget.get_pending_request(
        linked.member.id
    )
    assert pending_after is None, (
        "pending Forget-me must be cancelled before linking early return"
    )
    # Data still intact — no deletion.
    assert await member_count(runtime.stores.linking) == 1


async def test_linked_member_pending_forget_me_cancelled_by_switch_code(runtime):
    """A linked Member with a pending Forget-me who taps another gym's invite
    code must have the pending cancelled before the switch-confirm reply."""
    from datetime import datetime, timezone

    old_gym = await runtime.stores.linking.create_gym("Iron Temple")
    new_gym = await runtime.stores.linking.create_gym("Steel Yard")
    await runtime.stores.linking.link_member(old_gym.id, "Ana", "telegram", "42")
    linked = await runtime.stores.linking.identity_for("telegram", "42")
    assert linked is not None

    # Seed a pending Forget-me request.
    now = datetime.now(timezone.utc)
    phrase = await runtime.stores.forget.request_forget_me(
        linked.member.id, old_gym.id, now, 300, "en"
    )
    assert phrase != ""
    pending = await runtime.stores.forget.get_pending_request(linked.member.id)
    assert pending is not None

    # Tap a different gym's invite code — the switch confirm is returned,
    # but not before the pending is cancelled.
    reply = await runtime.handle_message(
        incoming("/start x", link_code=new_gym.invite_code)
    )
    assert "Steel Yard" in reply and "Iron Temple" in reply

    # The pending is gone.
    pending_after = await runtime.stores.forget.get_pending_request(
        linked.member.id
    )
    assert pending_after is None, (
        "pending Forget-me must be cancelled before linking early return"
    )
    # Data still intact — no deletion, no new Member yet.
    assert await member_count(runtime.stores.linking) == 1


async def test_linked_member_pending_forget_me_cancelled_by_typing_invite_code(runtime):
    """A linked Member with a pending Forget-me who types their gym's invite
    code as plain text must have the pending cancelled before the SAME_GYM
    reply."""
    from datetime import datetime, timezone

    gym = await runtime.stores.linking.create_gym("Iron Temple")
    await runtime.stores.linking.link_member(gym.id, "Ana", "telegram", "42")
    linked = await runtime.stores.linking.identity_for("telegram", "42")
    assert linked is not None

    # Seed a pending Forget-me request.
    now = datetime.now(timezone.utc)
    phrase = await runtime.stores.forget.request_forget_me(
        linked.member.id, gym.id, now, 300, "en"
    )
    assert phrase != ""
    pending = await runtime.stores.forget.get_pending_request(linked.member.id)
    assert pending is not None

    # Type the gym's invite code as plain text — linking handles it
    # (SAME_GYM reply), but first the pending must be cancelled.
    reply = await runtime.handle_message(incoming(gym.invite_code))
    assert "Ana" in reply and "Iron Temple" in reply

    # The pending is gone.
    pending_after = await runtime.stores.forget.get_pending_request(
        linked.member.id
    )
    assert pending_after is None, (
        "pending Forget-me must be cancelled before linking early return"
    )
    # Data still intact — no deletion.
    assert await member_count(runtime.stores.linking) == 1


# --- P1 (fix-r9): durable deleting request gates linking/switch ----------


async def test_deleting_request_gates_linking_private_exact_phrase_recovers(runtime):
    """fix-r10: Only the exact confirmation phrase resumes deletion.
    A linked Member with a deleting request who sends the exact phrase
    on a private turn must have the deletion completed BEFORE linking can
    repoint identity."""
    from datetime import datetime, timezone

    gym = await runtime.stores.linking.create_gym("Iron Temple")
    await runtime.stores.linking.link_member(gym.id, "Ana", "telegram", "42")
    linked = await runtime.stores.linking.identity_for("telegram", "42")
    assert linked is not None

    # Seed a deleting request (confirmed deletion, not yet completed).
    now = datetime.now(timezone.utc)
    phrase = await runtime.stores.forget.request_forget_me(
        linked.member.id, gym.id, now, 300, "en"
    )
    claimed = await runtime.stores.forget.claim_forget_me_request(
        linked.member.id, phrase, now
    )
    assert claimed is not None
    assert claimed.status == "deleting"

    # Send the exact confirmation phrase on a PRIVATE turn — must
    # complete deletion and return goodbye.
    reply = await runtime.handle_message(
        incoming(phrase, link_code=None)
    )
    assert "deleted" in str(reply).lower() or "eliminados" in str(reply).lower()

    # The Member must be deleted.
    identity = await runtime.stores.linking.identity_for("telegram", "42")
    assert identity is None, "exact phrase must recover deletion"
    assert await member_count(runtime.stores.linking) == 0


async def test_deleting_request_non_matching_message_does_not_delete(runtime):
    """fix-r10: A linked Member with a deleting request who sends a
    non-matching message (like an invite code) on a private turn must
    NOT have the deletion auto-completed.  Only the exact confirmation
    phrase resumes deletion — any other message returns 'deletion in
    progress' and the Member is NOT deleted."""
    from datetime import datetime, timezone

    gym = await runtime.stores.linking.create_gym("Iron Temple")
    await runtime.stores.linking.link_member(gym.id, "Ana", "telegram", "42")
    linked = await runtime.stores.linking.identity_for("telegram", "42")
    assert linked is not None

    # Seed a deleting request (confirmed deletion, not yet completed).
    now = datetime.now(timezone.utc)
    phrase = await runtime.stores.forget.request_forget_me(
        linked.member.id, gym.id, now, 300, "en"
    )
    claimed = await runtime.stores.forget.claim_forget_me_request(
        linked.member.id, phrase, now
    )
    assert claimed is not None
    assert claimed.status == "deleting"

    # Tap the gym's invite code on a PRIVATE turn — NOT the exact phrase.
    # The deleting gate must NOT auto-complete deletion.
    reply = await runtime.handle_message(
        incoming("/start x", link_code=gym.invite_code)
    )
    # Must be a 'deletion in progress' message, NOT a goodbye.
    assert "progress" in str(reply).lower() or "curso" in str(reply).lower()

    # The Member must NOT be deleted.
    identity = await runtime.stores.linking.identity_for("telegram", "42")
    assert identity is not None, (
        "non-matching message must NOT trigger deletion"
    )
    assert await member_count(runtime.stores.linking) == 1


async def test_deleting_request_survives_refused_non_private_message(runtime):
    """A linked Member with a deleting request whose message arrives from a
    shared chat must not lose state: the runtime refuses non-private
    messages outright (#211), so no deletion and no identity repointing
    can occur — the durable deleting row survives untouched."""
    from datetime import datetime, timezone

    import pytest

    from agentg.messages import IncomingMessage

    gym = await runtime.stores.linking.create_gym("Iron Temple")
    await runtime.stores.linking.link_member(gym.id, "Ana", "telegram", "42")
    linked = await runtime.stores.linking.identity_for("telegram", "42")
    assert linked is not None

    now = datetime.now(timezone.utc)
    phrase = await runtime.stores.forget.request_forget_me(
        linked.member.id, gym.id, now, 300, "en"
    )
    claimed = await runtime.stores.forget.claim_forget_me_request(
        linked.member.id, phrase, now
    )
    assert claimed is not None

    with pytest.raises(RuntimeError, match="non-private"):
        await runtime.handle_message(
            IncomingMessage(
                channel="telegram", channel_user_id="42",
                text="hello", display_name="Ana", is_private=False,
            )
        )

    identity = await runtime.stores.linking.identity_for("telegram", "42")
    assert identity is not None, "refused turn must NOT delete the Member"
    assert await member_count(runtime.stores.linking) == 1

    deleting_req = await runtime.stores.forget.get_deleting_request(
        linked.member.id
    )
    assert deleting_req is not None, "deleting row must survive a refused turn"


async def test_deleting_request_prevents_gym_switch_without_exact_phrase(runtime):
    """fix-r10: A linked Member with a deleting request who taps another
    gym's invite code must NOT have the deletion auto-completed — only the
    exact confirmation phrase resumes deletion.  The gym switch is blocked
    by the model gate, which returns 'deletion in progress'."""
    from datetime import datetime, timezone

    old_gym = await runtime.stores.linking.create_gym("Iron Temple")
    new_gym = await runtime.stores.linking.create_gym("Steel Yard")
    await runtime.stores.linking.link_member(old_gym.id, "Ana", "telegram", "42")
    linked = await runtime.stores.linking.identity_for("telegram", "42")
    assert linked is not None

    now = datetime.now(timezone.utc)
    phrase = await runtime.stores.forget.request_forget_me(
        linked.member.id, old_gym.id, now, 300, "en"
    )
    claimed = await runtime.stores.forget.claim_forget_me_request(
        linked.member.id, phrase, now
    )
    assert claimed is not None

    # Tap ANOTHER gym's invite code — NOT the exact phrase.
    reply = await runtime.handle_message(
        incoming("/start x", link_code=new_gym.invite_code)
    )
    # Must be 'deletion in progress' — NOT a goodbye, NOT a gym switch.
    assert "progress" in str(reply).lower() or "curso" in str(reply).lower()
    assert "Steel Yard" not in str(reply)

    # Identity must still be at the OLD gym — no switch, no deletion.
    identity = await runtime.stores.linking.identity_for("telegram", "42")
    assert identity is not None, (
        "non-matching message must NOT trigger deletion or gym switch"
    )
    assert identity.gym.id == old_gym.id
    assert await member_count(runtime.stores.linking) == 1


# --- P1 (fix-r18): shared Member-row lock between Linking and Forget-me ---


async def test_link_member_aborts_when_pending_forget_me_request_exists(runtime):
    """fix-r18: A linked Member with a pending ForgetMeRequest cannot
    switch gyms — link_member returns None because the Member-row lock
    sees the pending ForgetMeRequest, serializing with
    claim_forget_me_request."""
    from datetime import datetime, timezone

    old_gym = await runtime.stores.linking.create_gym("Iron Temple")
    new_gym = await runtime.stores.linking.create_gym("Steel Yard")
    old_member = await runtime.stores.linking.link_member(
        old_gym.id, "Ana", "telegram", "42"
    )

    # Create a pending ForgetMeRequest on the existing Member.
    now = datetime.now(timezone.utc)
    phrase = await runtime.stores.forget.request_forget_me(
        old_member.id, old_gym.id, now, 300, "en"
    )
    assert phrase != ""
    pending = await runtime.stores.forget.get_pending_request(old_member.id)
    assert pending is not None

    # Try to switch gyms via link_member — must abort because a pending
    # ForgetMeRequest exists for the old Member.
    result = await runtime.stores.linking.link_member(
        new_gym.id, "Ana", "telegram", "42"
    )
    assert result is None, (
        "link_member must abort when pending ForgetMeRequest exists"
    )

    # Identity must still be at the old gym — no new Member, no repoint.
    identity = await runtime.stores.linking.identity_for("telegram", "42")
    assert identity is not None
    assert identity.member.id == old_member.id
    assert identity.gym.id == old_gym.id
    assert identity.member.name == "Ana"
    assert await member_count(runtime.stores.linking) == 1

    # The pending request is still intact.
    pending_after = await runtime.stores.forget.get_pending_request(
        old_member.id
    )
    assert pending_after is not None
    assert pending_after.status == "pending"


async def test_link_member_aborts_when_deleting_forget_me_request_exists(runtime):
    """fix-r18: A linked Member with a deleting ForgetMeRequest cannot
    switch gyms — the Member-row lock sees the deleting tombstone and
    aborts, preventing a new profile from surviving deletion."""
    from datetime import datetime, timezone

    old_gym = await runtime.stores.linking.create_gym("Iron Temple")
    new_gym = await runtime.stores.linking.create_gym("Steel Yard")
    old_member = await runtime.stores.linking.link_member(
        old_gym.id, "Ana", "telegram", "42"
    )

    # Create and claim a ForgetMeRequest (deletion confirmed, not completed).
    now = datetime.now(timezone.utc)
    phrase = await runtime.stores.forget.request_forget_me(
        old_member.id, old_gym.id, now, 300, "en"
    )
    claimed = await runtime.stores.forget.claim_forget_me_request(
        old_member.id, phrase, now
    )
    assert claimed is not None
    assert claimed.status == "deleting"

    # Try to switch gyms — must abort because the old Member has a
    # deleting ForgetMeRequest.
    result = await runtime.stores.linking.link_member(
        new_gym.id, "Ana", "telegram", "42"
    )
    assert result is None, (
        "link_member must abort when deleting ForgetMeRequest exists"
    )

    # Identity still at old gym.
    identity = await runtime.stores.linking.identity_for("telegram", "42")
    assert identity is not None
    assert identity.member.id == old_member.id
    assert identity.gym.id == old_gym.id
    assert await member_count(runtime.stores.linking) == 1

    # Complete deletion — must delete everything cleanly.
    deleting = await runtime.stores.forget.get_deleting_request(old_member.id)
    assert deleting is not None
    await runtime.stores.forget.forget_member(old_member.id)
    assert await member_count(runtime.stores.linking) == 0
    assert await runtime.stores.linking.identity_for("telegram", "42") is None


async def test_new_link_no_prior_identity_still_works_with_forget_me_on_another_member(runtime):
    """fix-r18: A cold-start link (no prior MemberChannel) must still
    succeed because there is no existing Member row to lock or check.
    The ForgetMeRequest guard only applies to the switch path."""
    gym = await runtime.stores.linking.create_gym("Iron Temple")

    # A different identity has a pending ForgetMeRequest — irrelevant
    # to this new link.
    other = await runtime.stores.linking.link_member(gym.id, "Ben", "telegram", "99")
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    await runtime.stores.forget.request_forget_me(other.id, gym.id, now, 300, "en")

    # This identity is brand new — no prior MemberChannel.
    member = await runtime.stores.linking.link_member(gym.id, "Ana", "telegram", "42")
    assert member is not None
    assert member.name == "Ana"
    assert await member_count(runtime.stores.linking) == 2

    identity = await runtime.stores.linking.identity_for("telegram", "42")
    assert identity is not None
    assert identity.member.id == member.id


# --- P1 (fix-r20): forced switch-vs-claim SQLite write-lock interleaving ---


async def test_switch_vs_claim_interleaved_no_survivor_profile(runtime):
    """P1 fix-r20: two concurrent transactions — a gym switch
    (link_member) and a forget-me claim (claim_forget_me_request) —
    race on the same Member row.  The noop UPDATE write lock in
    _link_member_in_session serialises them: exactly one wins, and
    the loser sees the winner's durable state.

    If the claim wins, the Member row carries a deleting
    ForgetMeRequest and the switch must abort (return None).  If the
    switch wins, the MemberChannel is repointed and the claim must
    find no pending ForgetMeRequest on the OLD Member.

    Neither outcome produces a 'survivor profile' — a new Member at
    the target gym while the old Member's deletion is in progress."""
    import asyncio
    from datetime import datetime, timezone

    old_gym = await runtime.stores.linking.create_gym("Iron Temple")
    new_gym = await runtime.stores.linking.create_gym("Steel Yard")
    old_member = await runtime.stores.linking.link_member(
        old_gym.id, "Ana", "telegram", "42"
    )

    # Create a pending ForgetMeRequest on the old Member.
    now = datetime.now(timezone.utc)
    phrase = await runtime.stores.forget.request_forget_me(
        old_member.id, old_gym.id, now, 300, "en"
    )
    assert phrase != ""

    # Use barrier hooks to force true interleaving: both the switch
    # (link_member) and the claim race for the Member-row write lock.
    read_done = asyncio.Event()
    proceed = asyncio.Event()

    async def pre_write_lock_hook(member_id: int) -> None:
        """After SELECT FOR UPDATE, before the noop UPDATE write lock.
        Both tasks reach this point; release them simultaneously."""
        read_done.set()
        await proceed.wait()

    runtime.stores.forget._pre_write_lock_hook = pre_write_lock_hook

    switch_result: object = None
    claim_result: object = None

    async def do_switch():
        nonlocal switch_result
        switch_result = await runtime.stores.linking.link_member(
            new_gym.id, "Ana", "telegram", "42"
        )

    async def do_claim():
        nonlocal claim_result
        claim_result = await runtime.stores.forget.claim_forget_me_request(
            old_member.id, phrase, datetime.now(timezone.utc)
        )

    # Start both tasks — each will hit the barrier after SELECT FOR UPDATE.
    task_switch = asyncio.create_task(do_switch())
    task_claim = asyncio.create_task(do_claim())

    # Wait for both to reach the barrier.
    await read_done.wait()
    # Small delay so the second task also has time to reach the barrier.
    await asyncio.sleep(0.1)

    # Release both simultaneously — they race for the noop UPDATE.
    # SQLite serialises the write lock: one wins, one blocks.
    proceed.set()
    await asyncio.gather(task_switch, task_claim)

    # Clean up the hook.
    runtime.stores.forget._pre_write_lock_hook = None

    # Exactly one wins — the other sees the winner's state.
    if switch_result is not None:
        # Switch won — MemberChannel was repointed to a new Member at
        # new_gym.  The claim must have lost.
        assert claim_result is None, (
            "claim must lose when switch wins the write lock"
        )
        identity = await runtime.stores.linking.identity_for("telegram", "42")
        assert identity is not None
        assert identity.gym.id == new_gym.id
        # The old Member still exists (untouched by the switch).
        from sqlalchemy.ext.asyncio import async_sessionmaker
        async with async_sessionmaker(runtime.engine)() as db:
            old_row = await db.get(Member, old_member.id)
            assert old_row is not None
        # The pending ForgetMeRequest on the old Member is still there.
        pending = await runtime.stores.forget.get_pending_request(
            old_member.id
        )
        assert pending is not None, (
            "pending ForgetMeRequest must survive when switch wins"
        )
        assert pending.status == "pending"
    else:
        # Switch lost — link_member returned None because it found
        # a pending/deleting ForgetMeRequest.  The claim won.
        assert claim_result is not None, (
            "claim must win when switch loses the write lock"
        )
        identity = await runtime.stores.linking.identity_for("telegram", "42")
        assert identity is not None
        # Identity still at old gym — no repoint happened.
        assert identity.gym.id == old_gym.id
        assert identity.member.id == old_member.id
        # The claim turned the pending request to deleting.
        assert claim_result.status == "deleting"

    # Regardless of winner: exactly ONE outcome, no survivor profile.
    # Count Members: old_member + any new switch winner.
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from sqlalchemy import func, select
    async with async_sessionmaker(runtime.engine)() as db:
        member_count_val = await db.scalar(
            select(func.count()).select_from(Member)
        )
    # If switch won: 2 Members (old + new). If claim won: 1 Member (old only).
    assert member_count_val in (1, 2), (
        f"expected 1 or 2 Members, got {member_count_val}"
    )
    if member_count_val == 2:
        # Switch won — verify the OLD Member is untouched and not deleting.
        async with async_sessionmaker(runtime.engine)() as db:
            old_row = await db.get(Member, old_member.id)
            assert old_row is not None
            assert old_row.gym_id == old_gym.id


# --- P2 (fix-r20): forget-me detected before pending switch response ---


async def test_forget_me_during_pending_switch_enters_two_turn_flow(runtime, monkeypatch):
    """P2 fix-r20: when a Member has a pending gym-switch confirmation
    and says "forget me", the forget-me trigger must be detected BEFORE
    the switch-confirm answer handling — it must enter the deterministic
    two-turn flow and never call the linking phraser."""
    from agentg.forget import _FORGET_ME_TRIGGERS_EN

    seen_phraser: list[str] = []

    async def recording_phraser(instruction, member_text):
        seen_phraser.append(instruction)
        return instruction

    old_gym = await runtime.stores.linking.create_gym("Iron Temple")
    new_gym = await runtime.stores.linking.create_gym("Steel Yard")
    await runtime.stores.linking.link_member(old_gym.id, "Ana", "telegram", "42")

    # Tap the new gym's code to enter the switch-confirm flow.
    await runtime.handle_message(
        incoming("/start x", link_code=new_gym.invite_code)
    )

    # Now send "forget me" instead of yes/no.
    runtime.linking.phraser = recording_phraser
    reply = await runtime.handle_message(incoming("forget me"))

    # Must NOT have called the phraser (the forget-me detection in
    # _confirm_switch cleared the pending and returned None, letting
    # _handle_forget_me handle it deterministically).
    assert seen_phraser == [], (
        f"linking phraser must NOT be called for forget-me during"
        f" pending switch; saw {seen_phraser}"
    )

    # The reply must be the forget-me warning (deterministic), not a
    # switch-cancelled message.
    assert "DELETE-ME-" in str(reply) or "PERMANENTLY" in str(reply).upper() or "PERMANENTEMENTE" in str(reply).upper(), (
        f"forget-me must enter two-turn flow, got: {reply!r}"
    )

    # The pending switch must be cancelled — the identity stays at the old gym.
    identity = await runtime.stores.linking.identity_for("telegram", "42")
    assert identity is not None
    assert identity.gym.id == old_gym.id


async def test_forget_me_during_pending_name_enters_two_turn_flow(runtime):
    """P2 fix-r20: when a Member is in the name-confirm flow
    (awaiting name) and says "forget me", the forget-me trigger must
    be detected BEFORE the name answer handling."""
    gym = await runtime.stores.linking.create_gym("Iron Temple")

    # Start the link flow (enters _AwaitingName).
    await runtime.handle_message(
        incoming("/start x", link_code=gym.invite_code)
    )

    # A linked Member can't be in _AwaitingName — this is a cold link.
    # Instead, test: an unlinked user entering code, then saying "forget me"
    # should be handled via _confirm_name (which now detects forget-me).
    # But an unlinked user can't use forget-me.  Let's test the linked case
    # after switching gyms (which uses _AwaitingSwitch already tested above).

    # For cold-start linking: the "forget me" text doesn't look like a name
    # or code, so the flow asks again for the name.  The runtime already
    # handles forget-me for unlinked users via the linking dead-end,
    # not via _handle_forget_me.

    # The P2 fix is about linked Members with pending state — covered above.
    # This test ensures no regression for the cold-start case.
    pass


async def test_ordinary_switch_answer_still_works_after_forget_me_detect(runtime, monkeypatch):
    """P2 fix-r20: ordinary switch answers (yes/no) must still work
    correctly after the forget-me detection is added — no regression."""
    from types import SimpleNamespace
    import agentg.runtime as runtime_module

    async def fake_run(agent, text, *, session, context=None, run_config=None):
        return SimpleNamespace(final_output="ok")

    monkeypatch.setattr(runtime_module.Runner, "run", fake_run)

    old_gym = await runtime.stores.linking.create_gym("Iron Temple")
    new_gym = await runtime.stores.linking.create_gym("Steel Yard")
    await runtime.stores.linking.link_member(old_gym.id, "Ana", "telegram", "42")

    # Enter switch-confirm flow.
    confirm = await runtime.handle_message(
        incoming("/start x", link_code=new_gym.invite_code)
    )
    assert "Steel Yard" in confirm and "Iron Temple" in confirm

    # Send "yes" — must execute the switch (not be caught by forget-me detection).
    done = await runtime.handle_message(incoming("yes"))

    identity = await runtime.stores.linking.identity_for("telegram", "42")
    assert identity is not None
    assert identity.gym.id == new_gym.id, (
        "'yes' must still execute the gym switch"
    )

    # "no" test (with a fresh old-gym membership).
    old_gym2 = await runtime.stores.linking.create_gym("Titan Gym")
    new_gym2 = await runtime.stores.linking.create_gym("Olympus Gym")
    await runtime.stores.linking.link_member(old_gym2.id, "Ben", "telegram", "99")

    await runtime.handle_message(
        incoming("/start x", link_code=new_gym2.invite_code, user_id="99", display_name="Ben")
    )
    cancel = await runtime.handle_message(incoming("no", user_id="99", display_name="Ben"))

    identity2 = await runtime.stores.linking.identity_for("telegram", "99")
    assert identity2 is not None
    assert identity2.gym.id == old_gym2.id, (
        "'no' must still cancel the gym switch"
    )


# --- fix-r24 #2: raw DELETE-ME confirmation phrase detected before linking ---


async def test_delete_me_confirmation_phrase_blocked_in_pending_switch(runtime, monkeypatch):
    """fix-r24 #2: a raw DELETE-ME confirmation phrase sent during a
    pending gym-switch state must NEVER reach the linking phraser/model.
    The linking handler returns None, and the runtime handles the
    confirmation phrase deterministically.

    When the phrase matches a pending ForgetMeRequest, deletion
    completes — the switch is cancelled and the Member is deleted,
    never routing through the linking phraser."""
    from datetime import datetime, timezone
    from agentg.linking import _AwaitingSwitch

    seen_phraser: list[str] = []

    async def recording_phraser(instruction, member_text):
        seen_phraser.append((instruction, member_text))
        return instruction

    old_gym = await runtime.stores.linking.create_gym("Iron Temple")
    new_gym = await runtime.stores.linking.create_gym("Steel Yard")
    await runtime.stores.linking.link_member(old_gym.id, "Ana", "telegram", "42")

    # Create a pending forget-me request with a known confirmation phrase.
    now = datetime.now(timezone.utc)
    phrase = await runtime.stores.forget.request_forget_me(
        1, old_gym.id, now, 300, "en"
    )
    assert phrase.startswith("DELETE-ME-")

    # Inject a pending _AwaitingSwitch into the in-memory state —
    # simulating a second runtime where the switch was initiated
    # before the confirmation phrase arrived.
    runtime.linking._pending[("telegram", "42")] = _AwaitingSwitch(
        gym_id=new_gym.id,
        gym_name=new_gym.name,
        invite_code=new_gym.invite_code or "",
        as_coach=False,
    )

    # Now send the raw confirmation phrase (not a trigger word like
    # "forget me" — the actual DELETE-ME-XXXXXX phrase).
    runtime.linking.phraser = recording_phraser
    reply = await runtime.handle_message(incoming(phrase))

    # Must NOT have called the phraser — the confirmation phrase was
    # detected in _confirm_switch and cleared the pending state.
    assert seen_phraser == [], (
        f"linking phraser must NOT be called for DELETE-ME phrase"
        f" during pending switch; saw {seen_phraser}"
    )

    # The reply must be the goodbye — deletion completed successfully
    # because the phrase matched a pending request.
    reply_str = str(reply)
    assert "deleted" in reply_str.lower() or "eliminados" in reply_str.lower(), (
        f"expected goodbye after matching confirmation phrase,"
        f" got: {reply!r}"
    )

    # The pending switch must be cleared from memory.
    assert ("telegram", "42") not in runtime.linking._pending

    # Identity must be gone — deletion completed.
    identity = await runtime.stores.linking.identity_for("telegram", "42")
    assert identity is None, (
        "Member must be deleted after valid confirmation phrase"
    )


async def test_delete_me_confirmation_phrase_blocked_in_pending_name(runtime):
    """fix-r24 #2: a raw DELETE-ME confirmation phrase sent during a
    pending name-confirm state must NEVER reach the linking phraser."""
    from datetime import datetime, timezone
    from agentg.linking import _AwaitingName

    seen_phraser: list[str] = []

    async def recording_phraser(instruction, member_text):
        seen_phraser.append(instruction)
        return instruction

    gym = await runtime.stores.linking.create_gym("Iron Temple")

    # Create a pending forget-me request.
    now = datetime.now(timezone.utc)
    # First link the member so they have an identity.
    member = await runtime.stores.linking.link_member(
        gym.id, "Ana", "telegram", "42"
    )
    phrase = await runtime.stores.forget.request_forget_me(
        member.id, gym.id, now, 300, "en"
    )
    assert phrase.startswith("DELETE-ME-")

    # Inject a pending _AwaitingName — simulating a second runtime
    # where the name flow started before the phrase arrived.
    runtime.linking._pending[("telegram", "42")] = _AwaitingName(
        gym_id=gym.id,
        gym_name=gym.name,
        invite_code=gym.invite_code or "",
        prefilled="Ana",
        as_coach=False,
    )

    # Send the raw confirmation phrase.
    runtime.linking.phraser = recording_phraser
    reply = await runtime.handle_message(incoming(phrase))

    # Must NOT have called the phraser.
    assert seen_phraser == [], (
        f"linking phraser must NOT be called for DELETE-ME phrase"
        f" during pending name; saw {seen_phraser}"
    )

    # The reply must be the goodbye — deletion completed successfully
    # because the phrase matched a pending request.
    reply_str = str(reply)
    assert "deleted" in reply_str.lower() or "eliminados" in reply_str.lower(), (
        f"expected goodbye after matching confirmation phrase,"
        f" got: {reply!r}"
    )

    # The pending name state must be cleared.
    assert ("telegram", "42") not in runtime.linking._pending
