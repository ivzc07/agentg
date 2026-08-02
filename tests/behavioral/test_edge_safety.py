"""Edge/safety stratum: safety flags, forget-me, gym switch, check-in prefs."""

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


def _extract_confirmation_phrase(reply: str) -> str | None:
    """Pull the DELETE-ME-XXXXXX phrase out of a forget-me warning reply."""
    for line in reply.splitlines():
        stripped = line.strip()
        if stripped.startswith("DELETE-ME-"):
            return stripped
    return None


async def test_pain_report_flags_the_coach_with_a_deep_link(tmp_path):
    async with ConversationHarness.create(
        tmp_path, dashboard_base_url="https://dash.test"
    ) as h:
        await h.linked_member()
        await h.add_coach()

        # No consent ask (issue #101): one turn, straight to the flag.
        await h.say(
            "sharp pain in my shoulder when I press",
            steps=[
                tool(
                    "flag_to_coach",
                    summary="sharp shoulder pain on press",
                ),
                tool(
                    "remember_note",
                    kind="injury",
                    text="sharp shoulder pain on press",
                ),
                message("Flagged your coach and noted it."),
            ],
        )

        # Two messages per coach: the heads-up, then the one-time link alone.
        assert {(c, u) for c, u, _t in h.notifier.sent} == {("telegram", "7")}
        heads = [t for _c, _u, t in h.notifier.sent if "/login/" not in t]
        links = [t for _c, _u, t in h.notifier.sent if "/login/" in t]
        assert len(heads) == 1 and len(links) == 1
        assert "shoulder" in heads[0].lower()
        assert links[0].startswith("https://dash.test/login/")
        tokens = await h.login_tokens()
        assert any(t.next_path == f"/members/{h.member_id}" for t in tokens)
        notes = await h.stores.notes.active(h.member_id)
        safety = [n for n in notes if n.kind == "safety"]
        assert any("shoulder" in n.text.lower() for n in safety)


async def test_forget_me_wipes_every_member_row(tmp_path):
    """Two-turn forget-me: request, then confirm with exact phrase (issue #212)."""
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

        # Turn 1: request forget-me (pre-model — no model steps needed).
        request_reply = await h.say("forget me")
        # English trigger → English warning (ADR-0002 mirror).
        assert "permanently" in request_reply.lower()
        # The reply must include a confirmation phrase like DELETE-ME-XXXXXX.
        phrase = _extract_confirmation_phrase(request_reply)
        assert phrase is not None, f"no confirmation phrase in reply: {request_reply!r}"
        # Data must still be intact after request.
        assert await _count(h._engine, Member, id=member_id) == 1

        # Turn 2: confirm with the exact phrase (pre-model — no model steps).
        confirm_reply = await h.say(phrase)
        # Goodbye mirrors the trigger language (English).
        assert "deleted" in confirm_reply.lower()

        # Everything must be gone.
        assert await _count(h._engine, Member, id=member_id) == 0
        assert await _count(h._engine, MemberChannel, member_id=member_id) == 0
        assert await _count(h._engine, Session, member_id=member_id) == 0
        assert await _count(h._engine, MemberNote, member_id=member_id) == 0
        assert await _count(h._engine, Routine, member_id=member_id) == 0
        assert await h.stores.linking.identity_for("telegram", "42") is None
        # The SDK session must be residue-free.
        session = h.runtime.session_for_member(member_id)
        assert await session.get_items() == []


async def test_forget_me_wrong_phrase_cancels_and_leaves_data(tmp_path):
    """A wrong phrase cancels the request without deleting (issue #212)."""
    async with ConversationHarness.create(tmp_path) as h:
        await h.linked_member()
        await h.seed_closed_session("bench 60 8,8,8")
        member_id = h.member_id

        # Request forget-me.
        request_reply = await h.say("delete my account")
        # English trigger → English warning.
        assert "permanently" in request_reply.lower()
        assert await _count(h._engine, Member, id=member_id) == 1

        # Send a wrong phrase — the model runs normally.
        await h.say(
            "no wait keep my data",
            steps=[message("Nothing deleted — your data is safe.")],
        )

        assert await _count(h._engine, Member, id=member_id) == 1
        assert await h.stores.training.last_sets(member_id, "bench press") is not None


async def test_forget_me_group_message_ignored(tmp_path):
    """Group messages must not trigger or confirm forget-me (issue #212)."""
    async with ConversationHarness.create(tmp_path) as h:
        await h.linked_member()
        await h.seed_closed_session("bench 60 8,8,8")
        member_id = h.member_id

        # "forget me" in a group must pass through to the model.
        await h.say(
            "forget me",
            is_group=True,
            steps=[message("I can help with your training — what's on today?")],
        )
        # No pending request was created.
        pending = await h.stores.forget.get_pending_request(member_id)
        assert pending is None
        assert await _count(h._engine, Member, id=member_id) == 1


async def test_forget_me_retry_after_cancel(tmp_path):
    """After a cancelled request, asking again issues a fresh phrase (issue #212)."""
    async with ConversationHarness.create(tmp_path) as h:
        await h.linked_member()
        await h.seed_closed_session("bench 60 8,8,8")
        member_id = h.member_id

        # First request.
        reply1 = await h.say("forget me")
        phrase1 = _extract_confirmation_phrase(reply1)
        assert phrase1 is not None

        # Cancel by sending something else.
        await h.say(
            "actually never mind",
            steps=[message("No problem — your data is safe.")],
        )
        assert await _count(h._engine, Member, id=member_id) == 1

        # Second request issues a different phrase.
        reply2 = await h.say("forget me")
        phrase2 = _extract_confirmation_phrase(reply2)
        assert phrase2 is not None
        assert phrase1 != phrase2, "each request must get a fresh phrase"

        # Confirm with the second phrase.
        await h.say(phrase2)
        assert await _count(h._engine, Member, id=member_id) == 0


async def test_forget_me_phrase_from_old_request_does_not_delete(tmp_path):
    """After a request is cancelled, its old phrase must not delete (issue #212)."""
    async with ConversationHarness.create(tmp_path) as h:
        await h.linked_member()
        await h.seed_closed_session("bench 60 8,8,8")
        member_id = h.member_id

        reply1 = await h.say("forget me")
        phrase1 = _extract_confirmation_phrase(reply1)
        assert phrase1 is not None

        # Cancel by requesting again (which replaces the old request).
        reply2 = await h.say("forget me")
        phrase2 = _extract_confirmation_phrase(reply2)
        assert phrase2 is not None

        # Sending the old phrase must not delete.
        await h.say(
            phrase1,
            steps=[message("Hey! What can I help you with today?")],
        )
        assert await _count(h._engine, Member, id=member_id) == 1

        # The pending request should have been cancelled when the old phrase
        # didn't match.
        pending = await h.stores.forget.get_pending_request(member_id)
        assert pending is None


async def test_forget_me_group_message_clears_pending_without_deletion(tmp_path):
    """A group message when a forget-me request is pending must clear the
    pending intent without deleting data (issue #212)."""
    async with ConversationHarness.create(tmp_path) as h:
        await h.linked_member()
        await h.seed_closed_session("bench 60 8,8,8")
        member_id = h.member_id

        # Request forget-me (private).
        request_reply = await h.say("forget me")
        # English trigger → English warning.
        assert "permanently" in request_reply.lower()
        phrase = _extract_confirmation_phrase(request_reply)
        assert phrase is not None
        # Pending request exists.
        pending = await h.stores.forget.get_pending_request(member_id)
        assert pending is not None

        # A group message must clear the pending request without deletion.
        await h.say(
            "hey group",
            is_group=True,
            steps=[message("Group training — what's everyone working on today?")],
        )

        # Pending request is cancelled.
        pending = await h.stores.forget.get_pending_request(member_id)
        assert pending is None
        # Data still intact.
        assert await _count(h._engine, Member, id=member_id) == 1
        assert await h.stores.training.last_sets(member_id, "bench press") is not None


async def test_forget_me_expired_request_cancels_and_falls_through_to_model(tmp_path):
    """An expired forget-me request must not delete — the runtime cancels
    the pending intent silently and falls through to the model (issue #212)."""
    from datetime import datetime, timezone

    async with ConversationHarness.create(tmp_path) as h:
        await h.linked_member()
        await h.seed_closed_session("bench 60 8,8,8")
        member_id = h.member_id

        # Create an expired pending request directly.
        past = datetime(2020, 1, 1, tzinfo=timezone.utc)
        phrase = await h.stores.forget.request_forget_me(
            member_id, h.gym_id, past, 1
        )

        # Confirm the pending exists but is expired.
        pending = await h.stores.forget.get_pending_request(member_id)
        assert pending is not None

        # Sending the confirmation phrase on an *expired* request must
        # not delete — the runtime cancels the pending and falls through
        # to the model (which runs normally).
        await h.say(
            phrase,
            steps=[message("Hey! What can I help you with today?")],
        )

        # Data still intact.
        assert await _count(h._engine, Member, id=member_id) == 1
        assert await h.stores.training.last_sets(member_id, "bench press") is not None
        # Pending intent cleared.
        pending = await h.stores.forget.get_pending_request(member_id)
        assert pending is None


async def test_forget_me_expired_request_any_message_falls_through(tmp_path):
    """Any message on an expired forget-me request must cancel the pending
    silently and let the model run — no deletion (issue #212)."""
    from datetime import datetime, timezone

    async with ConversationHarness.create(tmp_path) as h:
        await h.linked_member()
        await h.seed_closed_session("bench 60 8,8,8")
        member_id = h.member_id

        # Create an expired pending request.
        past = datetime(2020, 1, 1, tzinfo=timezone.utc)
        await h.stores.forget.request_forget_me(
            member_id, h.gym_id, past, 1
        )

        # Any message — even a new forget-me trigger — on an expired
        # request must cancel first, then re-evaluate (the trigger text
        # below will issue a fresh request).
        request_reply = await h.say("forget me")
        # English trigger → English warning.
        assert "permanently" in request_reply.lower()
        # The old expired pending is gone; a fresh request was created.
        pending = await h.stores.forget.get_pending_request(member_id)
        assert pending is not None
        # Data still intact.
        assert await _count(h._engine, Member, id=member_id) == 1

        # Now confirm with the fresh phrase to prove it works.
        phrase = _extract_confirmation_phrase(request_reply)
        assert phrase is not None
        await h.say(phrase)
        assert await _count(h._engine, Member, id=member_id) == 0


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
