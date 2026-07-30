"""Coach invite stratum: the coach- prefixed code promotes in place (issue #104).

End-to-end through the runtime: deep-link taps and typed codes, with the
Agent's scripted turns proving the coach flag reaches the per-turn snapshot.
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from agentg.models import Gym, Member
from behavioral.harness import ConversationHarness, message


async def _count(engine, model, **where) -> int:
    async with async_sessionmaker(engine)() as db:
        q = select(func.count()).select_from(model)
        for col, val in where.items():
            q = q.where(getattr(model, col) == val)
        return int(await db.scalar(q) or 0)


async def _gym(h: ConversationHarness) -> Gym:
    """The harness's primary Gym row (``linked_member`` keeps only its id)."""
    async with async_sessionmaker(h._engine)() as db:
        gym = await db.get(Gym, h.gym_id)
        assert gym is not None
        return gym


async def test_newcomer_joining_via_coach_code_becomes_a_coach(tmp_path):
    async with ConversationHarness.create(tmp_path) as h:
        gym = await h.create_gym("Iron Temple")

        # Linking flow — no model steps.
        ask = await h.say(
            "/start x",
            link_code=gym.coach_invite_code,
            channel_user_id="99",
            display_name="Sam",
        )
        assert "Iron Temple" in ask
        assert await _count(h._engine, Member) == 0  # not before the name confirm

        greet = await h.say("yes", channel_user_id="99", display_name="Sam")

        # The welcome is coach-aware: /dashboard, no Intake started.
        assert "/dashboard" in greet
        linked = await h.stores.linking.identity_for("telegram", "99")
        assert linked is not None
        assert linked.gym.id == gym.id
        assert linked.member.name == "Sam" and linked.member.is_coach is True

        # The very next chat turn reaches the Agent with the Coach role.
        await h.say(
            "hey",
            steps=[message("Welcome, coach!")],
            channel_user_id="99",
            display_name="Sam",
        )
        instructions = h.model.calls[-1]["system_instructions"]
        assert "Coach (coach tools available): Sam, at Iron Temple" in instructions


async def test_existing_member_typing_the_coach_code_is_promoted_in_place(tmp_path):
    async with ConversationHarness.create(tmp_path) as h:
        await h.linked_member(name="Dani", gym_name="Iron Temple")
        await h.seed_closed_session("bench 60 8,8,8")
        member_id = h.member_id
        gym = await _gym(h)

        reply = await h.say(f"  {gym.coach_invite_code.upper()} ")  # typed, any case

        assert "/dashboard" in reply  # coach-aware, not a re-link greeting
        assert await _count(h._engine, Member) == 1  # promoted, no new row
        linked = await h.stores.linking.identity_for("telegram", "42")
        assert linked is not None
        assert linked.member.id == member_id and linked.member.is_coach is True
        # Training history is untouched by the promotion.
        last = await h.stores.training.last_sets(member_id, "bench press")
        assert last is not None and last["weight"] == 60.0

        await h.say("what's on today", steps=[message("Your members await.")])
        instructions = h.model.calls[-1]["system_instructions"]
        assert "Coach (coach tools available): Dani, at Iron Temple" in instructions


async def test_other_gyms_coach_code_switches_gyms_arriving_coach_flagged(tmp_path):
    async with ConversationHarness.create(tmp_path) as h:
        await h.linked_member(name="Dani", gym_name="Iron Temple")
        await h.seed_closed_session("bench 60 8,8,8")
        old_member_id = h.member_id
        new_gym = await h.create_gym("Copper Gym")

        # The normal gym switch: explicit confirm, then a fresh row.
        confirm = await h.say("/start x", link_code=new_gym.coach_invite_code)
        assert "Copper Gym" in confirm and "Iron Temple" in confirm
        assert await _count(h._engine, Member) == 1  # nothing switched yet

        await h.say("yes")

        linked = await h.stores.linking.identity_for("telegram", "42")
        assert linked is not None
        assert linked.gym.id == new_gym.id
        assert linked.member.id != old_member_id
        assert linked.member.name == "Dani" and linked.member.is_coach is True
        # The old gym keeps its Member and their training.
        assert await _count(h._engine, Member, id=old_member_id) == 1
        old_sets = await h.stores.training.last_sets(old_member_id, "bench press")
        assert old_sets is not None and old_sets["weight"] == 60.0
        assert await h.stores.training.last_sets(linked.member.id, "bench press") is None


async def test_regenerated_coach_code_invalidates_pending_link_and_unflags_no_one(tmp_path):
    async with ConversationHarness.create(tmp_path) as h:
        await h.linked_member(name="Dani", gym_name="Iron Temple")
        coach = await h.add_coach(name="Coach Sam", channel_user_id="7")
        gym = await _gym(h)

        # A newcomer is mid-link on the coach code when ops regenerates it.
        await h.say(
            "/start x",
            link_code=gym.coach_invite_code,
            channel_user_id="99",
            display_name="New Person",
        )
        await h.stores.linking.regenerate_coach_invite_code(h.gym_id)

        reply = await h.say("yes", channel_user_id="99", display_name="New Person")

        assert "Iron Temple" not in reply  # expired code, gyms are not named
        assert await _count(h._engine, Member) == 2  # the link never happened
        assert await h.stores.linking.identity_for("telegram", "99") is None
        # Regenerating never unflags anyone.
        linked_coach = await h.stores.linking.identity_for("telegram", "7")
        assert linked_coach is not None
        assert linked_coach.member.id == coach.id and linked_coach.member.is_coach is True


async def test_regenerated_coach_code_invalidates_a_pending_switch(tmp_path):
    async with ConversationHarness.create(tmp_path) as h:
        await h.linked_member(name="Dani", gym_name="Iron Temple")
        old_member_id = h.member_id
        new_gym = await h.create_gym("Copper Gym")

        await h.say("/start x", link_code=new_gym.coach_invite_code)
        await h.stores.linking.regenerate_coach_invite_code(new_gym.id)

        reply = await h.say("yes")

        assert "Iron Temple" in reply  # reassured they're still set up
        linked = await h.stores.linking.identity_for("telegram", "42")
        assert linked is not None
        assert linked.member.id == old_member_id and linked.member.is_coach is False
        assert await _count(h._engine, Member) == 1  # the switch never happened


async def test_member_invite_code_still_links_as_a_plain_member(tmp_path):
    async with ConversationHarness.create(tmp_path) as h:
        gym = await h.create_gym("Iron Temple")

        await h.say(
            "/start x",
            link_code=gym.invite_code,
            channel_user_id="99",
            display_name="Sam",
        )
        greet = await h.say("yes", channel_user_id="99", display_name="Sam")

        assert "/dashboard" not in greet  # the member welcome, not the coach one
        linked = await h.stores.linking.identity_for("telegram", "99")
        assert linked is not None
        assert linked.gym.id == gym.id and linked.member.is_coach is False

        await h.say(
            "I'm here",
            steps=[message("Let's go.")],
            channel_user_id="99",
            display_name="Sam",
        )
        instructions = h.model.calls[-1]["system_instructions"]
        assert "Member: Sam, at Iron Temple" in instructions
        assert "coach tools available" not in instructions


async def test_member_retapping_the_member_link_stays_a_plain_member(tmp_path):
    async with ConversationHarness.create(tmp_path) as h:
        await h.linked_member(name="Dani", gym_name="Iron Temple")
        gym = await _gym(h)

        reply = await h.say("/start x", link_code=gym.invite_code)

        assert "Iron Temple" in reply and "Dani" in reply  # same-gym greeting
        assert "/dashboard" not in reply
        assert await _count(h._engine, Member) == 1
        linked = await h.stores.linking.identity_for("telegram", "42")
        assert linked is not None and linked.member.is_coach is False
