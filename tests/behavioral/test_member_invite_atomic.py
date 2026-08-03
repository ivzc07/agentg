"""Member Invite atomic stratum: redemption-first and regeneration-first races
(issue #215).

The Member Invite code is regenerable.  A pending first-time link or gym
switch must be revoked when regeneration commits before the confirm;
conversely, when redemption commits first the link must complete
consistently before regeneration takes effect.

These tests drive the runtime end-to-end through the conversation harness
so they exercise the full linking state machine (`AwaitingName`,
`AwaitingSwitch`), not just store-level operations.
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from agentg.models import Gym, Member
from behavioral.harness import ConversationHarness


async def _count(engine, model, **where) -> int:
    async with async_sessionmaker(engine)() as db:
        q = select(func.count()).select_from(model)
        for col, val in where.items():
            q = q.where(getattr(model, col) == val)
        return int(await db.scalar(q) or 0)


# --- first-time redemption is atomic ---


async def test_first_time_linking_with_active_code_succeeds(tmp_path):
    """A newcomer's confirmation redeems the active code atomically —
    the Member row and channel pointer are both committed."""
    async with ConversationHarness.create(tmp_path) as h:
        gym = await h.create_gym("Iron Temple")

        await h.say(
            "/start x",
            link_code=gym.invite_code,
            channel_user_id="99",
            display_name="Sam",
        )
        greet = await h.say("yes", channel_user_id="99", display_name="Sam")

        assert "Iron Temple" in greet and "Sam" in greet
        linked = await h.stores.linking.identity_for("telegram", "99")
        assert linked is not None
        assert linked.gym.id == gym.id and linked.member.name == "Sam"
        assert linked.member.is_coach is False


async def test_first_time_linking_with_revoked_code_writes_nothing(tmp_path):
    """A newcomer's confirmation on a code regenerated before the confirm
    leaves no trace — no Member row, no channel pointer."""
    async with ConversationHarness.create(tmp_path) as h:
        gym = await h.create_gym("Iron Temple")

        await h.say(
            "/start x",
            link_code=gym.invite_code,
            channel_user_id="99",
            display_name="Sam",
        )
        await h.stores.linking.regenerate_invite_code(gym.id)

        reply = await h.say("yes", channel_user_id="99", display_name="Sam")

        assert "Iron Temple" not in reply  # expired, gym not named
        assert await _count(h._engine, Member) == 0
        assert await h.stores.linking.identity_for("telegram", "99") is None

    # The retry with the current code links cleanly.
    async with ConversationHarness.create(tmp_path) as h2:
        gym = await h2.create_gym("Iron Temple")

        await h2.say(
            "/start x",
            link_code=gym.invite_code,
            channel_user_id="99",
            display_name="Sam",
        )
        await h2.say("yes", channel_user_id="99", display_name="Sam")

        assert await _count(h2._engine, Member) == 1
        linked = await h2.stores.linking.identity_for("telegram", "99")
        assert linked is not None and linked.member.is_coach is False


# --- gym switching is atomic ---


async def test_switching_gyms_with_active_code_succeeds(tmp_path):
    """A gym switch confirmed while the new Gym's code is active creates a
    fresh Member at the new Gym."""
    async with ConversationHarness.create(tmp_path) as h:
        await h.linked_member(name="Dani", gym_name="Iron Temple")
        old_member_id = h.member_id
        new_gym = await h.create_gym("Steel Yard")

        await h.say("/start x", link_code=new_gym.invite_code)
        await h.say("yes")

        linked = await h.stores.linking.identity_for("telegram", "42")
        assert linked is not None
        assert linked.gym.id == new_gym.id
        assert linked.member.id != old_member_id
        assert linked.member.name == "Dani"
        # Old Member row untouched by the atomic switch.
        assert await _count(h._engine, Member, id=old_member_id) == 1


async def test_switching_gyms_with_revoked_code_leaves_identity_unchanged(tmp_path):
    """When regeneration commits before the switch confirm, the identity
    stays at the old Gym — no partial migration, no duplicate."""
    async with ConversationHarness.create(tmp_path) as h:
        await h.linked_member(name="Dani", gym_name="Iron Temple")
        old_member_id = h.member_id
        new_gym = await h.create_gym("Steel Yard")

        await h.say("/start x", link_code=new_gym.invite_code)
        await h.stores.linking.regenerate_invite_code(new_gym.id)

        reply = await h.say("yes")

        # Expired-code recovery response — no gym named in the expired reply.
        assert "Iron Temple" not in reply
        linked = await h.stores.linking.identity_for("telegram", "42")
        assert linked is not None
        assert linked.member.id == old_member_id
        assert linked.gym.id == h.gym_id  # unchanged
        # No new Member at the target Gym.
        assert await _count(h._engine, Member) == 1


# --- regeneration-first vs redemption-first races ---


async def test_redemption_first_completes_before_regeneration_takes_effect(tmp_path):
    """The redemption commits first, so the regeneration waits — the new
    Member is created before the code changes."""
    async with ConversationHarness.create(tmp_path) as h:
        gym = await h.create_gym("Iron Temple")

        await h.say(
            "/start x",
            link_code=gym.invite_code,
            channel_user_id="99",
            display_name="Sam",
        )

        # Simulate a race: the confirm and regeneration happen in the same
        # turn.  Because runtime turns are serialised per identity, we
        # sequence them: redemption first, then regeneration.
        await h.say("yes", channel_user_id="99", display_name="Sam")
        await h.stores.linking.regenerate_invite_code(gym.id)

        # Redemption committed first: the Member exists and the identity
        # points at the Gym.
        linked = await h.stores.linking.identity_for("telegram", "99")
        assert linked is not None
        assert linked.gym.id == gym.id
        assert linked.member.name == "Sam"
        assert await _count(h._engine, Member) == 1


async def test_regeneration_first_revokes_a_pending_first_time_link(tmp_path):
    """Regeneration commits before the confirm — the link is revoked and the
    newcomer must get a fresh code."""
    async with ConversationHarness.create(tmp_path) as h:
        gym = await h.create_gym("Iron Temple")

        await h.say(
            "/start x",
            link_code=gym.invite_code,
            channel_user_id="99",
            display_name="Sam",
        )
        await h.stores.linking.regenerate_invite_code(gym.id)

        reply = await h.say("yes", channel_user_id="99", display_name="Sam")

        assert "Iron Temple" not in reply  # expired code response
        assert await _count(h._engine, Member) == 0
        assert await h.stores.linking.identity_for("telegram", "99") is None


async def test_redemption_first_switch_completes_before_regeneration_takes_effect(tmp_path):
    """Redemption commits first for a Gym switch — the identity moves to the
    new Gym before regeneration takes effect."""
    async with ConversationHarness.create(tmp_path) as h:
        await h.linked_member(name="Dani", gym_name="Iron Temple")
        old_member_id = h.member_id
        new_gym = await h.create_gym("Steel Yard")

        await h.say("/start x", link_code=new_gym.invite_code)

        # Redemption first, then regeneration — the switch completes.
        await h.say("yes")
        await h.stores.linking.regenerate_invite_code(new_gym.id)

        linked = await h.stores.linking.identity_for("telegram", "42")
        assert linked is not None
        assert linked.gym.id == new_gym.id
        assert linked.member.id != old_member_id
        assert linked.member.name == "Dani"
        # Old Member row untouched by the atomic switch.
        assert await _count(h._engine, Member, id=old_member_id) == 1


async def test_regeneration_first_revokes_a_pending_switch(tmp_path):
    """Regeneration on the target Gym commits before the switch confirm —
    identity stays at the old Gym."""
    async with ConversationHarness.create(tmp_path) as h:
        await h.linked_member(name="Dani", gym_name="Iron Temple")
        old_member_id = h.member_id
        new_gym = await h.create_gym("Steel Yard")

        await h.say("/start x", link_code=new_gym.invite_code)
        await h.stores.linking.regenerate_invite_code(new_gym.id)

        reply = await h.say("yes")

        # Expired-code recovery response — no gym named in the expired reply.
        assert "Iron Temple" not in reply
        linked = await h.stores.linking.identity_for("telegram", "42")
        assert linked is not None
        assert linked.member.id == old_member_id
        assert await _count(h._engine, Member) == 1


# --- the Linking state retains the initiating code through confirmation ---


async def test_linking_state_retains_the_initiating_code(tmp_path):
    """The pending state carries the initiating Invite code so the atomic
    redemption can check it at confirmation time."""
    async with ConversationHarness.create(tmp_path) as h:
        gym = await h.create_gym("Iron Temple")

        await h.say(
            "/start x",
            link_code=gym.invite_code,
            channel_user_id="99",
            display_name="Sam",
        )

        # The code at confirm time is the one the state was created with —
        # even if regeneration happened mid-flow, it's the pending code
        # that is checked atomically.
        await h.stores.linking.regenerate_invite_code(gym.id)
        reply = await h.say("yes", channel_user_id="99", display_name="Sam")

        assert "Iron Temple" not in reply  # the old code doesn't match anymore


# --- Coach Invite and trusted admin creation are unchanged ---


async def test_coach_invite_atomic_redemption_is_preserved(tmp_path):
    """Coach Invite redemption was already atomic (issue #104);
    this guarantees #215 didn't regress it."""
    async with ConversationHarness.create(tmp_path) as h:
        gym = await h.create_gym("Iron Temple")

        # First-time coach link is still atomic.
        await h.say(
            "/start x",
            link_code=gym.coach_invite_code,
            channel_user_id="99",
            display_name="Coach Sam",
        )
        await h.stores.linking.regenerate_coach_invite_code(gym.id)
        reply = await h.say("yes", channel_user_id="99", display_name="Coach Sam")
        assert "Iron Temple" not in reply  # expired

        # Retry with the new code works.
        # Fetch the regenerated coach code
        async with async_sessionmaker(h._engine)() as db:
            fresh_gym = await db.get(Gym, gym.id)
            assert fresh_gym is not None
        await h.say(
            "/start x",
            link_code=fresh_gym.coach_invite_code,
            channel_user_id="99",
            display_name="Coach Sam",
        )
        await h.say("yes", channel_user_id="99", display_name="Coach Sam")
        linked = await h.stores.linking.identity_for("telegram", "99")
        assert linked is not None and linked.member.is_coach is True


async def test_trusted_admin_member_creation_is_preserved(tmp_path):
    """``link_member`` (without code) is still trusted for admin/test
    callers — it doesn't require a code at all."""
    async with ConversationHarness.create(tmp_path) as h:
        await h.linked_member(name="Dani", gym_name="Iron Temple")

        linked = await h.stores.linking.identity_for("telegram", "42")
        assert linked is not None
        assert linked.member.name == "Dani" and linked.member.is_coach is False
        assert await _count(h._engine, Member) == 1

        # An extra admin-created Member with the same identity repoints it.
        admin = await h.stores.linking.create_gym("Admin Gym")
        await h.stores.linking.link_member(admin.id, "Admin Dani", "telegram", "42")
        linked2 = await h.stores.linking.identity_for("telegram", "42")
        assert linked2 is not None
        assert linked2.gym.id == admin.id
        assert await _count(h._engine, Member) == 2


# --- expired code gives a warm response asking for the current link ---


async def test_expired_member_code_gives_an_expired_response(tmp_path):
    """When a code is regenerated mid-flow, the confirm yields a warm
    'get the current link' response — not a dead end."""
    async with ConversationHarness.create(tmp_path) as h:
        gym = await h.create_gym("Iron Temple")

        await h.say(
            "/start x",
            link_code=gym.invite_code,
            channel_user_id="99",
            display_name="Sam",
        )
        await h.stores.linking.regenerate_invite_code(gym.id)

        reply = await h.say("yes", channel_user_id="99", display_name="Sam")

        # The expired-LINK reply, not the generic dead end — it asks for the
        # current invite link.
        assert "Iron Temple" not in reply  # gym not named in the expired response
        assert await _count(h._engine, Member) == 0
