"""Edge/safety stratum: consent, forget-me, gym switch, check-in prefs."""

from __future__ import annotations

from datetime import date

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from agentg.models import Member, MemberChannel, MemberNote, Routine, Session, Set
from behavioral.harness import ConversationHarness, message, tool


async def _count(engine, model, **where) -> int:
    async with async_sessionmaker(engine)() as db:
        q = select(func.count()).select_from(model)
        for col, val in where.items():
            q = q.where(getattr(model, col) == val)
        return int(await db.scalar(q) or 0)


async def test_pain_report_with_consent_flags_coach(tmp_path):
    async with ConversationHarness.create(tmp_path) as h:
        await h.linked_member()
        await h.add_coach()

        await h.say(
            "sharp pain in my shoulder when I press",
            steps=[message("Want me to flag this to your coach?")],
        )
        await h.say(
            "yes please",
            steps=[
                tool(
                    "flag_to_coach",
                    summary="sharp shoulder pain on press",
                    share_with_coach=True,
                ),
                tool(
                    "remember_note",
                    kind="injury",
                    text="sharp shoulder pain on press",
                ),
                message("Flagged your coach and noted it."),
            ],
        )

        assert len(h.notifier.sent) == 1
        channel, user_id, text = h.notifier.sent[0]
        assert channel == "telegram" and user_id == "7"
        assert "shoulder" in text.lower()
        notes = await h.stores.notes.active(h.member_id)
        assert any("shoulder" in n.text.lower() for n in notes)


async def test_pain_report_without_consent_logs_but_does_not_ping(tmp_path):
    async with ConversationHarness.create(tmp_path) as h:
        await h.linked_member()
        await h.add_coach()

        await h.say(
            "my knee flared up today",
            steps=[message("Want me to flag your coach?")],
        )
        await h.say(
            "no, keep it between us",
            steps=[
                tool(
                    "flag_to_coach",
                    summary="knee flare-up",
                    share_with_coach=False,
                ),
                message("Logged privately — coach not pinged."),
            ],
        )

        assert h.notifier.sent == []
        notes = await h.stores.notes.active(h.member_id)
        assert any("knee" in n.text.lower() or "Safety flag" in n.text for n in notes)


async def test_forget_me_wipes_every_member_row(tmp_path):
    async with ConversationHarness.create(tmp_path) as h:
        await h.linked_member()
        await h.seed_closed_session("bench 60 8,8,8")
        await h.stores.notes.remember(h.member_id, h.gym_id, "goal", "get strong")
        from agentg.routines import ExerciseSpec, WorkoutSpec

        await h.stores.routines.save_routine(
            h.member_id,
            h.gym_id,
            [
                WorkoutSpec(
                    weekday=0,
                    name="Push",
                    exercises=[ExerciseSpec("bench press", sets=3, reps="8")],
                )
            ],
        )
        member_id = h.member_id

        await h.say(
            "forget me",
            steps=[message("This permanently erases everything — sure?")],
        )
        await h.say(
            "yes, delete everything",
            steps=[
                tool("delete_my_data", confirm=True),
                message("Goodbye — you're wiped."),
            ],
        )

        assert await _count(h._engine, Member, id=member_id) == 0
        assert await _count(h._engine, MemberChannel, member_id=member_id) == 0
        assert await _count(h._engine, Session, member_id=member_id) == 0
        assert await _count(h._engine, MemberNote, member_id=member_id) == 0
        assert await _count(h._engine, Routine, member_id=member_id) == 0
        assert await h.stores.linking.identity_for("telegram", "42") is None


async def test_forget_me_declined_leaves_data_intact(tmp_path):
    async with ConversationHarness.create(tmp_path) as h:
        await h.linked_member()
        await h.seed_closed_session("bench 60 8,8,8")
        member_id = h.member_id

        await h.say(
            "forget me",
            steps=[message("This wipes everything — sure?")],
        )
        await h.say(
            "no wait keep my data",
            steps=[
                tool("delete_my_data", confirm=False),
                message("Nothing deleted."),
            ],
        )

        assert await _count(h._engine, Member, id=member_id) == 1
        assert await h.stores.training.last_sets(member_id, "bench press") is not None


async def test_gym_switch_creates_fresh_member_and_keeps_old(tmp_path):
    async with ConversationHarness.create(tmp_path) as h:
        await h.linked_member(name="Dani", gym_name="Iron Temple")
        await h.seed_closed_session("bench 60 8,8,8")
        old_member_id = h.member_id
        old_gym_id = h.gym_id
        new_gym = await h.create_gym("Copper Gym")

        # Linking flow — no model steps.
        await h.say("/start x", link_code=new_gym.invite_code)
        await h.say("yes")

        linked = await h.stores.linking.identity_for("telegram", "42")
        assert linked is not None
        assert linked.gym.id == new_gym.id
        assert linked.member.id != old_member_id
        assert linked.member.name == "Dani"

        # Old member row and their training stay put under the old gym.
        assert await _count(h._engine, Member, id=old_member_id) == 1
        old_sets = await h.stores.training.last_sets(old_member_id, "bench press")
        assert old_sets is not None and old_sets["weight"] == 60.0
        # New member has a clean slate.
        assert await h.stores.training.last_sets(linked.member.id, "bench press") is None
        assert linked.member.gym_id == new_gym.id
        assert old_gym_id != new_gym.id


async def test_stop_checkins_turns_state_off(tmp_path):
    async with ConversationHarness.create(tmp_path) as h:
        await h.linked_member()
        await h.say(
            "stop checking in on me",
            steps=[tool("stop_checkins"), message("Check-ins off.")],
        )
        state, until = await h.stores.checkins.get_state(h.member_id)
        assert state == "off"
        assert until is None


async def test_snooze_checkins_sets_date(tmp_path):
    async with ConversationHarness.create(tmp_path) as h:
        await h.linked_member()
        await h.say(
            "I'm traveling for two weeks",
            steps=[
                tool("snooze_checkins", until="2026-07-29"),
                message("Snoozed until the 29th."),
            ],
        )
        state, until = await h.stores.checkins.get_state(h.member_id)
        assert state == "snoozed"
        assert until == date(2026, 7, 29)


async def test_resume_checkins_turns_state_on(tmp_path):
    async with ConversationHarness.create(tmp_path) as h:
        await h.linked_member()
        await h.stores.checkins.turn_off(h.member_id)
        await h.say(
            "start checking in again",
            steps=[tool("resume_checkins"), message("Check-ins back on.")],
        )
        state, _ = await h.stores.checkins.get_state(h.member_id)
        assert state == "on"
